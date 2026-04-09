"""
Backend bridge to delimit-gateway core engine.

Adapter Boundary Contract v1.0:
- Pure translation layer: no governance logic here
- Deterministic error on failure (never swallow)
- Zero state (stateless between calls)
- No schema forking (gateway types are canonical)
"""

import sys
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.gateway_core")

# Add gateway root to path so we can import core modules
GATEWAY_ROOT = Path(__file__).resolve().parent.parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))


def _load_specs(spec_path: str) -> Dict[str, Any]:
    """Load an API spec (OpenAPI or JSON Schema) from a file path.

    Performs a non-fatal version compatibility check (LED-290) so that
    unknown OpenAPI versions log a warning instead of silently parsing.
    JSON Schema documents skip the OpenAPI version assert.
    """
    import yaml

    p = Path(spec_path)
    if not p.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")

    content = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        spec = yaml.safe_load(content)
    else:
        spec = json.loads(content)

    # LED-290: warn (non-fatal) if version is outside the validated set.
    # Only applies to OpenAPI/Swagger documents — bare JSON Schema files
    # have no "openapi"/"swagger" key and would otherwise trip the assert.
    try:
        if isinstance(spec, dict) and ("openapi" in spec or "swagger" in spec):
            from core.openapi_version import assert_supported
            assert_supported(spec, strict=False)
    except Exception as exc:  # pragma: no cover -- defensive only
        logger.debug("openapi version check skipped: %s", exc)

    return spec


# ---------------------------------------------------------------------------
# LED-713: JSON Schema spec-type dispatch helpers
# ---------------------------------------------------------------------------


def _spec_type(doc: Any) -> str:
    """Classify a loaded spec doc. 'openapi' or 'json_schema'."""
    from core.spec_detector import detect_spec_type
    t = detect_spec_type(doc)
    # Fallback to openapi for unknown so we never break existing flows.
    return "json_schema" if t == "json_schema" else "openapi"


def _json_schema_changes_to_dicts(changes: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "type": c.type.value,
            "path": c.path,
            "message": c.message,
            "is_breaking": c.is_breaking,
            "details": c.details,
        }
        for c in changes
    ]


def _json_schema_semver(changes: List[Any]) -> Dict[str, Any]:
    """Build an OpenAPI-compatible semver result from JSON Schema changes.

    Mirrors core.semver_classifier.classify_detailed shape so downstream
    consumers (PR comment, CI formatter, ledger) don't need to branch.
    """
    breaking = [c for c in changes if c.is_breaking]
    non_breaking = [c for c in changes if not c.is_breaking]
    if breaking:
        bump = "major"
    elif non_breaking:
        bump = "minor"
    else:
        bump = "none"
    return {
        "bump": bump,
        "is_breaking": bool(breaking),
        "counts": {
            "breaking": len(breaking),
            "non_breaking": len(non_breaking),
            "total": len(changes),
        },
    }


def _bump_semver_version(current: str, bump: str) -> Optional[str]:
    """Minimal semver bump for JSON Schema path (core.semver_classifier
    only understands OpenAPI ChangeType enums)."""
    if not current:
        return None
    try:
        parts = current.lstrip("v").split(".")
        major, minor, patch = (int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return current


def _run_json_schema_lint(
    old_doc: Dict[str, Any],
    new_doc: Dict[str, Any],
    current_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an evaluate_with_policy-compatible result for JSON Schema.

    Policy rules in Delimit are defined against OpenAPI ChangeType values,
    so they do not apply here. We return zero violations and rely on the
    breaking-change count + semver bump to drive the governance gate.
    """
    from core.json_schema_diff import JSONSchemaDiffEngine

    engine = JSONSchemaDiffEngine()
    changes = engine.compare(old_doc, new_doc)
    semver = _json_schema_semver(changes)

    if current_version:
        semver["current_version"] = current_version
        semver["next_version"] = _bump_semver_version(current_version, semver["bump"])

    breaking_count = semver["counts"]["breaking"]
    total = semver["counts"]["total"]

    decision = "pass"
    exit_code = 0
    # No policy rules apply to JSON Schema, but breaking changes still
    # flag MAJOR semver and the downstream gate uses that to block.
    # Mirror the shape of evaluate_with_policy so the action/CLI renderers
    # need no JSON Schema-specific branch.
    result: Dict[str, Any] = {
        "spec_type": "json_schema",
        "api_name": api_name or new_doc.get("title") or old_doc.get("title") or "JSON Schema",
        "decision": decision,
        "exit_code": exit_code,
        "violations": [],
        "summary": {
            "total_changes": total,
            "breaking_changes": breaking_count,
            "violations": 0,
            "errors": 0,
            "warnings": 0,
        },
        "all_changes": [
            {
                "type": c.type.value,
                "path": c.path,
                "message": c.message,
                "is_breaking": c.is_breaking,
            }
            for c in changes
        ],
        "semver": semver,
    }
    return result


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read JSONL entries from a file, skipping malformed lines."""
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    items.append(payload)
    except OSError:
        return []
    return items


def _query_project_ledger_fallback(ledger_path: Path) -> Optional[Dict[str, Any]]:
    """Fallback for project-local ledgers that use operations/strategy jsonl files."""
    if ledger_path.name != "events.jsonl":
        return None

    ledger_dir = ledger_path.parent
    operations = _read_jsonl(ledger_dir / "operations.jsonl")
    strategy = _read_jsonl(ledger_dir / "strategy.jsonl")
    combined = operations + strategy
    if not combined:
        return None

    latest = combined[-1]
    return {
        "path": str(ledger_path),
        "event_count": len(combined),
        "latest_event": latest,
        "storage_mode": "project_local_ledger",
        "ledger_files": [
            str(p)
            for p in (ledger_dir / "operations.jsonl", ledger_dir / "strategy.jsonl")
            if p.exists()
        ],
        "chain_valid": True,
    }


def run_spec_health(spec_path: str) -> Dict[str, Any]:
    """Score a single OpenAPI spec on quality dimensions.

    Returns overall score, letter grade, per-dimension scores,
    and actionable recommendations.
    """
    from core.spec_health import score_spec

    spec = _load_specs(spec_path)
    return score_spec(spec)


def run_lint(old_spec: str, new_spec: str, policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Run the full lint pipeline: diff + policy evaluation.

    This is the Tier 1 primary tool — combines diff detection with
    policy enforcement into a single pass/fail decision. Auto-detects
    spec type (OpenAPI vs JSON Schema, LED-713) and dispatches to the
    matching engine.
    """
    from core.policy_engine import evaluate_with_policy

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    # LED-713: JSON Schema dispatch. Policy rules are OpenAPI-specific,
    # so JSON Schema takes the no-policy (breaking-count + semver) path.
    if _spec_type(new) == "json_schema" or _spec_type(old) == "json_schema":
        return _run_json_schema_lint(old, new)

    return evaluate_with_policy(old, new, policy_file)


def run_diff(old_spec: str, new_spec: str) -> Dict[str, Any]:
    """Run diff engine only — no policy evaluation.

    Auto-detects OpenAPI vs JSON Schema and dispatches (LED-713).
    """
    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    if _spec_type(new) == "json_schema" or _spec_type(old) == "json_schema":
        from core.json_schema_diff import JSONSchemaDiffEngine
        engine = JSONSchemaDiffEngine()
        changes = engine.compare(old, new)
        breaking = [c for c in changes if c.is_breaking]
        return {
            "spec_type": "json_schema",
            "total_changes": len(changes),
            "breaking_changes": len(breaking),
            "changes": _json_schema_changes_to_dicts(changes),
        }

    from core.diff_engine_v2 import OpenAPIDiffEngine
    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)

    breaking = [c for c in changes if c.is_breaking]

    return {
        "spec_type": "openapi",
        "total_changes": len(changes),
        "breaking_changes": len(breaking),
        "changes": [
            {
                "type": c.type.value,
                "path": c.path,
                "message": c.message,
                "is_breaking": c.is_breaking,
                "details": c.details,
            }
            for c in changes
        ],
    }


def run_changelog(
    old_spec: str,
    new_spec: str,
    fmt: str = "markdown",
    version: str = "",
) -> Dict[str, Any]:
    """Generate a changelog from API spec changes.

    Uses the diff engine to detect changes, then formats them into
    a human-readable changelog grouped by category.
    """
    from datetime import datetime, timezone

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    # LED-713: dispatch on spec type. JSONSchemaChange / Change share the
    # (.type.value, .path, .message, .is_breaking) duck type.
    if _spec_type(new) == "json_schema" or _spec_type(old) == "json_schema":
        from core.json_schema_diff import JSONSchemaDiffEngine
        engine = JSONSchemaDiffEngine()
    else:
        from core.diff_engine_v2 import OpenAPIDiffEngine
        engine = OpenAPIDiffEngine()

    changes = engine.compare(old, new)

    # Categorize changes
    breaking = []
    features = []
    deprecations = []
    fixes = []

    for c in changes:
        entry = {
            "type": c.type.value,
            "path": c.path,
            "message": c.message,
            "is_breaking": c.is_breaking,
        }
        if c.type.value == "deprecated_added":
            deprecations.append(entry)
        elif c.is_breaking:
            breaking.append(entry)
        elif c.type.value in (
            "endpoint_added", "method_added", "optional_param_added",
            "response_added", "optional_field_added", "enum_value_added",
            "security_added",
        ):
            features.append(entry)
        else:
            fixes.append(entry)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    version_label = version or "Unreleased"

    if fmt == "json":
        return {
            "format": "json",
            "version": version_label,
            "date": date_str,
            "total_changes": len(changes),
            "sections": {
                "breaking_changes": breaking,
                "new_features": features,
                "deprecations": deprecations,
                "other_changes": fixes,
            },
        }

    if fmt == "keepachangelog":
        lines = [f"## [{version_label}] - {date_str}", ""]
        if breaking:
            lines.append("### Removed / Breaking")
            for e in breaking:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        if features:
            lines.append("### Added")
            for e in features:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        if deprecations:
            lines.append("### Deprecated")
            for e in deprecations:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        if fixes:
            lines.append("### Changed")
            for e in fixes:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        return {
            "format": "keepachangelog",
            "version": version_label,
            "date": date_str,
            "total_changes": len(changes),
            "changelog": "\n".join(lines),
        }

    if fmt == "github-release":
        lines = []
        if breaking:
            lines.append("## :warning: Breaking Changes")
            for e in breaking:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        if features:
            lines.append("## :rocket: New Features")
            for e in features:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        if deprecations:
            lines.append("## :no_entry_sign: Deprecations")
            for e in deprecations:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        if fixes:
            lines.append("## :wrench: Other Changes")
            for e in fixes:
                lines.append(f"- {e['message']} (`{e['path']}`)")
            lines.append("")
        return {
            "format": "github-release",
            "version": version_label,
            "date": date_str,
            "total_changes": len(changes),
            "changelog": "\n".join(lines),
        }

    # Default: markdown
    lines = [f"# Changelog — {version_label} ({date_str})", ""]
    if breaking:
        lines.append("## Breaking Changes")
        for e in breaking:
            lines.append(f"- **{e['type']}**: {e['message']} (`{e['path']}`)")
        lines.append("")
    if features:
        lines.append("## New Features")
        for e in features:
            lines.append(f"- **{e['type']}**: {e['message']} (`{e['path']}`)")
        lines.append("")
    if deprecations:
        lines.append("## Deprecations")
        for e in deprecations:
            lines.append(f"- **{e['type']}**: {e['message']} (`{e['path']}`)")
        lines.append("")
    if fixes:
        lines.append("## Other Changes")
        for e in fixes:
            lines.append(f"- **{e['type']}**: {e['message']} (`{e['path']}`)")
        lines.append("")
    return {
        "format": "markdown",
        "version": version_label,
        "date": date_str,
        "total_changes": len(changes),
        "changelog": "\n".join(lines),
    }


def run_changelog_from_git(
    repo_path: str = ".",
    version: str = "",
    fmt: str = "keepachangelog",
    since_tag: str = "",
    include_ledger: bool = True,
    output_file: str = "",
) -> Dict[str, Any]:
    """Generate a changelog from git commits and ledger items.

    Reads git log since the last tag (or a specified tag), categorizes
    commits by conventional-commit prefix, optionally pulls completed
    ledger items, and formats as Markdown.

    Works for ANY git repo, not just Delimit's own.
    """
    import re
    import subprocess
    from datetime import datetime, timezone

    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        return {"error": "not_a_git_repo", "message": f"{repo} is not a git repository."}

    # --- Resolve the base tag ---
    if since_tag:
        base_tag = since_tag
    else:
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=str(repo), capture_output=True, text=True, timeout=10,
            )
            base_tag = result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            base_tag = ""

    # --- Get git log ---
    git_log_cmd = ["git", "log", "--pretty=format:%H|%s|%an", "--no-merges"]
    if base_tag:
        git_log_cmd.append(f"{base_tag}..HEAD")
    try:
        result = subprocess.run(
            git_log_cmd, cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
        raw_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    except Exception as e:
        return {"error": "git_log_failed", "message": str(e)}

    # --- Get diff stats ---
    diff_stat_cmd = ["git", "diff", "--stat"]
    if base_tag:
        diff_stat_cmd.append(f"{base_tag}..HEAD")
    else:
        diff_stat_cmd.append("--cached")  # fallback: staged changes
    try:
        stat_result = subprocess.run(
            diff_stat_cmd, cwd=str(repo), capture_output=True, text=True, timeout=15,
        )
        stat_summary = stat_result.stdout.strip().split("\n")[-1] if stat_result.stdout.strip() else ""
    except Exception:
        stat_summary = ""

    # --- Parse and categorize commits ---
    # Conventional commit pattern: type(scope): message  OR  type: message
    cc_pattern = re.compile(
        r"^(?P<type>feat|fix|refactor|docs|test|tests|ci|chore|perf|style|build|revert)"
        r"(?:\([^)]*\))?:\s*(?P<msg>.+)$",
        re.IGNORECASE,
    )

    categories = {
        "feat": [],
        "fix": [],
        "refactor": [],
        "docs": [],
        "test": [],
        "ci": [],
        "chore": [],
        "other": [],
    }

    # Keyword fallback patterns for non-conventional commits
    keyword_map = [
        (re.compile(r"\b(add|feature|implement|new)\b", re.I), "feat"),
        (re.compile(r"\b(fix|bug|patch|resolve|close)\b", re.I), "fix"),
        (re.compile(r"\b(refactor|restructure|clean|simplify)\b", re.I), "refactor"),
        (re.compile(r"\b(doc|readme|comment|jsdoc)\b", re.I), "docs"),
        (re.compile(r"\b(test|spec|coverage|assert)\b", re.I), "test"),
        (re.compile(r"\b(ci|workflow|action|pipeline|deploy)\b", re.I), "ci"),
    ]

    commits_parsed = []
    for line in raw_lines:
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, subject, author = parts[0], parts[1], parts[2]

        m = cc_pattern.match(subject)
        if m:
            ctype = m.group("type").lower()
            if ctype in ("tests",):
                ctype = "test"
            msg = m.group("msg")
        else:
            # Keyword fallback
            ctype = "other"
            msg = subject
            for pattern, cat in keyword_map:
                if pattern.search(subject):
                    ctype = cat
                    break

        bucket = ctype if ctype in categories else "other"
        entry = {"sha": sha[:8], "message": msg, "author": author, "category": bucket}
        categories[bucket].append(entry)
        commits_parsed.append(entry)

    # --- Pull completed ledger items (if requested and ledger exists) ---
    ledger_items = []
    if include_ledger:
        try:
            ledger_dir = Path.home() / ".delimit" / "ledger"
            ops_file = ledger_dir / "operations.jsonl"
            if ops_file.exists():
                import json as _json
                items_raw = []
                for ln in ops_file.read_text().splitlines():
                    ln = ln.strip()
                    if ln:
                        try:
                            items_raw.append(_json.loads(ln))
                        except _json.JSONDecodeError:
                            continue

                # Build current state by replaying events
                state = {}
                for item in items_raw:
                    item_id = item.get("id", "")
                    if item.get("type") == "update":
                        if item_id in state:
                            if "status" in item:
                                state[item_id]["status"] = item["status"]
                    elif item_id:
                        state[item_id] = item

                # Find the timestamp of the base tag to filter ledger items
                tag_dt = None
                if base_tag:
                    try:
                        ts_result = subprocess.run(
                            ["git", "log", "-1", "--format=%aI", base_tag],
                            cwd=str(repo), capture_output=True, text=True, timeout=10,
                        )
                        if ts_result.returncode == 0 and ts_result.stdout.strip():
                            tag_dt = datetime.fromisoformat(ts_result.stdout.strip())
                    except Exception:
                        pass

                for item_id, item in state.items():
                    if item.get("status") == "done":
                        created = item.get("created_at", "")
                        # If we have a tag datetime, only include items created after it
                        if tag_dt and created:
                            try:
                                # Normalize "Z" suffix to "+00:00" for fromisoformat
                                created_norm = created.replace("Z", "+00:00") if created.endswith("Z") else created
                                created_dt = datetime.fromisoformat(created_norm)
                                if created_dt < tag_dt:
                                    continue
                            except (ValueError, TypeError):
                                pass  # If parsing fails, include the item
                        ledger_items.append({
                            "id": item_id,
                            "title": item.get("title", ""),
                            "priority": item.get("priority", ""),
                        })
        except Exception:
            pass  # Ledger is optional; failing silently is fine

    # --- Count stats ---
    test_commits = len(categories["test"])
    total_commits = len(commits_parsed)

    # Parse files changed from stat summary
    files_changed = 0
    insertions = 0
    deletions = 0
    if stat_summary:
        fc_match = re.search(r"(\d+) files? changed", stat_summary)
        ins_match = re.search(r"(\d+) insertions?\(\+\)", stat_summary)
        del_match = re.search(r"(\d+) deletions?\(-\)", stat_summary)
        if fc_match:
            files_changed = int(fc_match.group(1))
        if ins_match:
            insertions = int(ins_match.group(1))
        if del_match:
            deletions = int(del_match.group(1))

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    version_label = version or "Unreleased"

    stats = {
        "total_commits": total_commits,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "tests_added": test_commits,
        "base_tag": base_tag or "(initial)",
    }

    # --- Category display names (keepachangelog style) ---
    section_map = {
        "feat": "Added",
        "fix": "Fixed",
        "refactor": "Changed",
        "docs": "Documentation",
        "test": "Tests",
        "ci": "CI/CD",
        "chore": "Chores",
        "other": "Other",
    }

    # --- JSON format ---
    if fmt == "json":
        return {
            "format": "json",
            "version": version_label,
            "date": date_str,
            "stats": stats,
            "categories": {k: v for k, v in categories.items() if v},
            "ledger_items": ledger_items,
        }

    # --- Markdown formats ---
    lines = []
    if fmt == "github-release":
        # No top-level header for GH release (title comes from the release itself)
        pass
    else:
        lines.append(f"## [{version_label}] - {date_str}")
        lines.append("")

    for cat_key in ("feat", "fix", "refactor", "docs", "test", "ci", "chore", "other"):
        entries = categories[cat_key]
        if not entries:
            continue
        section_name = section_map[cat_key]
        lines.append(f"### {section_name}")
        for e in entries:
            lines.append(f"- {e['message']} ({e['sha']})")
        lines.append("")

    if ledger_items:
        lines.append("### Completed Ledger Items")
        for item in ledger_items:
            priority_tag = f"[{item['priority']}] " if item.get("priority") else ""
            lines.append(f"- **{item['id']}**: {priority_tag}{item['title']}")
        lines.append("")

    # Stats footer
    lines.append("### Stats")
    lines.append(f"- **Commits**: {total_commits}")
    lines.append(f"- **Files changed**: {files_changed}")
    lines.append(f"- **Insertions**: {insertions}(+) / {deletions}(-)")
    if test_commits:
        lines.append(f"- **Test commits**: {test_commits}")
    if base_tag:
        lines.append(f"- **Since**: {base_tag}")
    lines.append("")

    changelog_text = "\n".join(lines)

    # --- Write to file if requested ---
    wrote_file = ""
    if output_file:
        out_path = Path(output_file)
        if out_path.name == "CHANGELOG.md" and out_path.exists():
            # Prepend to existing CHANGELOG.md (keep old content)
            existing = out_path.read_text()
            # Insert after the first line if it starts with "# Changelog"
            if existing.startswith("# Changelog"):
                header_end = existing.index("\n") + 1
                new_content = existing[:header_end] + "\n" + changelog_text + existing[header_end:]
            else:
                new_content = "# Changelog\n\n" + changelog_text + "\n" + existing
            out_path.write_text(new_content)
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("# Changelog\n\n" + changelog_text)
        wrote_file = str(out_path)

    return {
        "format": fmt,
        "version": version_label,
        "date": date_str,
        "stats": stats,
        "total_commits": total_commits,
        "ledger_items_count": len(ledger_items),
        "changelog": changelog_text,
        "wrote_file": wrote_file,
    }


def run_policy(spec_files: List[str], policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate specs against governance policy without diffing."""
    from core.policy_engine import PolicyEngine

    engine = PolicyEngine(policy_file)

    return {
        "rules_loaded": len(engine.rules),
        "custom_rules": len(engine.custom_rules),
        "policy_file": policy_file,
        "template": engine.create_policy_template() if not policy_file else None,
    }


def simulate_policy(
    old_spec: str,
    new_spec: str,
    policy_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Run lint + policy across all presets in dry-run mode.

    Returns what would pass/fail under strict, default, and relaxed presets,
    plus the custom policy if provided. Nothing is enforced or recorded.
    """
    from core.policy_engine import PolicyEngine, POLICY_PRESETS, evaluate_with_policy
    from core.diff_engine_v2 import OpenAPIDiffEngine

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    # Run diff once (shared across all preset evaluations)
    diff_engine = OpenAPIDiffEngine()
    changes = diff_engine.compare(old, new)

    change_dicts = [
        {
            "type": c.type.value,
            "path": c.path,
            "message": c.message,
            "is_breaking": c.is_breaking,
        }
        for c in changes
    ]

    # Evaluate each preset
    preset_results: Dict[str, Any] = {}
    for preset in POLICY_PRESETS:
        engine = PolicyEngine(preset)
        violations = engine.evaluate(changes)

        has_errors = any(v.severity == "error" for v in violations)
        has_warnings = any(v.severity == "warning" for v in violations)

        if has_errors:
            decision = "fail"
        elif has_warnings:
            decision = "warn"
        else:
            decision = "pass"

        preset_results[preset] = {
            "decision": decision,
            "rules_loaded": len(engine.rules),
            "violations": [
                {
                    "rule": v.rule_id,
                    "name": v.rule_name,
                    "severity": v.severity,
                    "message": v.message,
                    "path": v.change.path,
                }
                for v in violations
            ],
            "errors": len([v for v in violations if v.severity == "error"]),
            "warnings": len([v for v in violations if v.severity == "warning"]),
        }

    # Evaluate custom policy if provided (in addition to presets)
    custom_result = None
    if policy_file:
        custom_engine = PolicyEngine(policy_file)
        custom_violations = custom_engine.evaluate(changes)

        has_errors = any(v.severity == "error" for v in custom_violations)
        has_warnings = any(v.severity == "warning" for v in custom_violations)

        if has_errors:
            custom_decision = "fail"
        elif has_warnings:
            custom_decision = "warn"
        else:
            custom_decision = "pass"

        custom_result = {
            "decision": custom_decision,
            "policy_file": policy_file,
            "rules_loaded": len(custom_engine.rules),
            "violations": [
                {
                    "rule": v.rule_id,
                    "name": v.rule_name,
                    "severity": v.severity,
                    "message": v.message,
                    "path": v.change.path,
                }
                for v in custom_violations
            ],
            "errors": len([v for v in custom_violations if v.severity == "error"]),
            "warnings": len([v for v in custom_violations if v.severity == "warning"]),
        }

    # Build comparison matrix
    comparison = {}
    for preset in POLICY_PRESETS:
        comparison[preset] = preset_results[preset]["decision"]
    if custom_result:
        comparison["custom"] = custom_result["decision"]

    return {
        "simulated": True,
        "dry_run": True,
        "summary": {
            "total_changes": len(changes),
            "breaking_changes": len([c for c in changes if c.is_breaking]),
        },
        "all_changes": change_dicts,
        "presets": preset_results,
        "custom_policy": custom_result,
        "comparison": comparison,
    }


def query_ledger(
    ledger_path: str,
    api_name: Optional[str] = None,
    repository: Optional[str] = None,
    validate_chain: bool = False,
) -> Dict[str, Any]:
    """Query the contract ledger."""
    from core.contract_ledger import ContractLedger

    ledger = ContractLedger(ledger_path)

    if not ledger.exists():
        return {"error": "Ledger not found", "path": ledger_path}

    result: Dict[str, Any] = {"path": ledger_path, "event_count": ledger.get_event_count()}
    if result["event_count"] == 0:
        fallback = _query_project_ledger_fallback(Path(ledger_path))
        if fallback:
            if api_name:
                fallback["events"] = [e for e in _read_jsonl(Path(ledger_path).parent / "operations.jsonl") + _read_jsonl(Path(ledger_path).parent / "strategy.jsonl") if e.get("api_name") == api_name]
            elif repository:
                fallback["events"] = [e for e in _read_jsonl(Path(ledger_path).parent / "operations.jsonl") + _read_jsonl(Path(ledger_path).parent / "strategy.jsonl") if e.get("repository") == repository]
            return fallback

    if validate_chain:
        try:
            ledger.validate_chain()
            result["chain_valid"] = True
        except Exception as e:
            result["chain_valid"] = False
            result["chain_error"] = str(e)

    if api_name:
        result["events"] = ledger.get_api_timeline(api_name)
    elif repository:
        result["events"] = ledger.get_events_by_repository(repository)
    else:
        latest = ledger.get_latest_event()
        result["latest_event"] = latest

    return result


def run_impact(api_name: str, dependency_file: Optional[str] = None) -> Dict[str, Any]:
    """Analyze downstream impact of an API change."""
    from core.dependency_graph import DependencyGraph
    from core.impact_analyzer import ImpactAnalyzer

    graph = DependencyGraph()
    if dependency_file:
        graph.load_from_file(dependency_file)

    analyzer = ImpactAnalyzer(graph)
    return analyzer.analyze(api_name)


def run_semver(
    old_spec: str,
    new_spec: str,
    current_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify the semver bump for a spec change.

    Returns detailed breakdown: bump level, per-category counts,
    and optionally the bumped version string. Auto-detects OpenAPI vs
    JSON Schema (LED-713).
    """
    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    # LED-713: JSON Schema path
    if _spec_type(new) == "json_schema" or _spec_type(old) == "json_schema":
        from core.json_schema_diff import JSONSchemaDiffEngine
        engine = JSONSchemaDiffEngine()
        changes = engine.compare(old, new)
        result = _json_schema_semver(changes)
        if current_version:
            result["current_version"] = current_version
            result["next_version"] = _bump_semver_version(current_version, result["bump"])
        return result

    from core.diff_engine_v2 import OpenAPIDiffEngine
    from core.semver_classifier import classify_detailed, bump_version, classify

    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)
    result = classify_detailed(changes)

    if current_version:
        bump = classify(changes)
        result["current_version"] = current_version
        result["next_version"] = bump_version(current_version, bump)

    return result


def run_explain(
    old_spec: str,
    new_spec: str,
    template: str = "developer",
    old_version: Optional[str] = None,
    new_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a human-readable explanation of API changes.

    Supports 7 templates: developer, team_lead, product, migration,
    changelog, pr_comment, slack.
    """
    from core.diff_engine_v2 import OpenAPIDiffEngine
    from core.explainer import explain, TEMPLATES

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)

    output = explain(
        changes,
        template=template,
        old_version=old_version,
        new_version=new_version,
        api_name=api_name,
    )

    return {
        "template": template,
        "available_templates": TEMPLATES,
        "output": output,
    }


def run_zero_spec(
    project_dir: str = ".",
    python_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Detect framework and extract OpenAPI spec from source code.

    Currently supports FastAPI. Returns the extracted spec or an error
    with guidance on how to fix it.
    """
    from core.zero_spec.detector import detect_framework, Framework
    from core.zero_spec.express_extractor import extract_express_spec
    from core.zero_spec.fastapi_extractor import extract_fastapi_spec
    from core.zero_spec.nestjs_extractor import extract_nestjs_spec

    info = detect_framework(project_dir)

    result: Dict[str, Any] = {
        "framework": info.framework.value,
        "confidence": info.confidence,
        "message": info.message,
    }

    if info.framework == Framework.FASTAPI:
        extraction = extract_fastapi_spec(
            info, project_dir, python_bin=python_bin
        )
        result.update(extraction)
        if extraction["success"] and info.app_locations:
            loc = info.app_locations[0]
            result["app_file"] = loc.file
            result["app_variable"] = loc.variable
            result["app_line"] = loc.line
    elif info.framework == Framework.NESTJS:
        extraction = extract_nestjs_spec(info, project_dir)
        result.update(extraction)
        if extraction["success"] and info.app_locations:
            loc = info.app_locations[0]
            result["app_file"] = loc.file
            result["app_variable"] = loc.variable
            result["app_line"] = loc.line
    elif info.framework == Framework.EXPRESS:
        extraction = extract_express_spec(info, project_dir)
        result.update(extraction)
        if extraction["success"] and info.app_locations:
            loc = info.app_locations[0]
            result["app_file"] = loc.file
            result["app_variable"] = loc.variable
            result["app_line"] = loc.line
    else:
        result["success"] = False
        result["error"] = "No supported API framework found. Provide an OpenAPI spec file."
        result["error_type"] = "no_framework"

    return result


def run_diff_report(
    old_spec: str,
    new_spec: str,
    fmt: str = "html",
    output_file: Optional[str] = None,
    policy_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a rich comparison report for two API spec versions.

    Runs the full analysis pipeline (diff, policy, semver, health) and
    produces a self-contained HTML report or structured JSON suitable
    for sharing across teams.

    Args:
        old_spec: Path to the baseline OpenAPI spec.
        new_spec: Path to the proposed OpenAPI spec.
        fmt: Output format -- "html" or "json".
        output_file: Optional path to write the report file.
        policy_file: Optional custom policy YAML.

    Returns:
        Dict with report content, metadata, and analysis results.
    """
    from datetime import datetime, timezone

    from core.policy_engine import PolicyEngine
    from core.semver_classifier import classify_detailed, classify
    from core.spec_health import score_spec
    from core.explainer import explain

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    # LED-713: JSON Schema dispatch — short-circuit to a minimal report
    # shape compatible with the JSON renderer (HTML renderer remains
    # OpenAPI-only; JSON Schema callers should use fmt="json").
    if _spec_type(new) == "json_schema" or _spec_type(old) == "json_schema":
        from core.json_schema_diff import JSONSchemaDiffEngine
        js_engine = JSONSchemaDiffEngine()
        js_changes = js_engine.compare(old, new)
        js_breaking = [c for c in js_changes if c.is_breaking]
        js_semver = _json_schema_semver(js_changes)
        now_js = datetime.now(timezone.utc)
        return {
            "format": fmt,
            "spec_type": "json_schema",
            "generated_at": now_js.isoformat(),
            "old_spec": old_spec,
            "new_spec": new_spec,
            "old_title": old.get("title", "") if isinstance(old, dict) else "",
            "new_title": new.get("title", "") if isinstance(new, dict) else "",
            "semver": js_semver,
            "changes": _json_schema_changes_to_dicts(js_changes),
            "breaking_count": len(js_breaking),
            "non_breaking_count": len(js_changes) - len(js_breaking),
            "total_changes": len(js_changes),
            "policy": {
                "decision": "pass",
                "violations": [],
                "errors": 0,
                "warnings": 0,
            },
            "health": None,
            "migration": "",
            "output_file": output_file,
            "note": "JSON Schema report (policy rules and HTML report are OpenAPI-only in v1)",
        }

    from core.diff_engine_v2 import OpenAPIDiffEngine

    # -- Diff --
    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)

    breaking = [c for c in changes if c.is_breaking]
    non_breaking = [c for c in changes if not c.is_breaking]

    change_dicts = [
        {
            "type": c.type.value,
            "path": c.path,
            "message": c.message,
            "is_breaking": c.is_breaking,
            "details": c.details,
        }
        for c in changes
    ]

    # -- Semver --
    semver = classify_detailed(changes)
    bump = classify(changes)

    # -- Policy --
    policy_engine = PolicyEngine(policy_file)
    violations = policy_engine.evaluate(changes)

    has_errors = any(v.severity == "error" for v in violations)
    has_warnings = any(v.severity == "warning" for v in violations)
    if has_errors:
        gate_decision = "fail"
    elif has_warnings:
        gate_decision = "warn"
    else:
        gate_decision = "pass"

    violation_dicts = [
        {
            "rule": v.rule_id,
            "name": v.rule_name,
            "severity": v.severity,
            "message": v.message,
            "path": v.change.path,
        }
        for v in violations
    ]

    # -- Spec health --
    old_health = score_spec(old)
    new_health = score_spec(new)

    # -- Migration guide (only if breaking changes exist) --
    migration_text = ""
    if breaking:
        try:
            old_ver = old.get("info", {}).get("version")
            new_ver = new.get("info", {}).get("version")
            migration_text = explain(
                changes,
                template="migration",
                old_version=old_ver,
                new_version=new_ver,
            )
        except Exception:
            migration_text = ""

    now = datetime.now(timezone.utc)
    report_data = {
        "generated_at": now.isoformat(),
        "old_spec": old_spec,
        "new_spec": new_spec,
        "old_version": old.get("info", {}).get("version", "unknown"),
        "new_version": new.get("info", {}).get("version", "unknown"),
        "old_title": old.get("info", {}).get("title", ""),
        "new_title": new.get("info", {}).get("title", ""),
        "semver": {
            "bump": semver["bump"],
            "is_breaking": semver["is_breaking"],
            "counts": semver["counts"],
        },
        "changes": change_dicts,
        "breaking_count": len(breaking),
        "non_breaking_count": len(non_breaking),
        "total_changes": len(changes),
        "policy": {
            "decision": gate_decision,
            "violations": violation_dicts,
            "errors": len([v for v in violations if v.severity == "error"]),
            "warnings": len([v for v in violations if v.severity == "warning"]),
        },
        "health": {
            "old": old_health,
            "new": new_health,
        },
        "migration_guide": migration_text,
    }

    if fmt == "json":
        wrote_file = ""
        if output_file:
            p = Path(output_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report_data, indent=2, default=str), encoding="utf-8")
            wrote_file = str(p)
        return {
            "format": "json",
            "wrote_file": wrote_file,
            "report": report_data,
        }

    # -- HTML generation --
    html = _render_diff_report_html(report_data)

    wrote_file = ""
    if output_file:
        p = Path(output_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        wrote_file = str(p)

    return {
        "format": "html",
        "wrote_file": wrote_file,
        "html": html,
        "summary": {
            "total_changes": report_data["total_changes"],
            "breaking_count": report_data["breaking_count"],
            "non_breaking_count": report_data["non_breaking_count"],
            "semver_bump": report_data["semver"]["bump"],
            "policy_decision": report_data["policy"]["decision"],
            "old_health_grade": old_health.get("grade", "?"),
            "new_health_grade": new_health.get("grade", "?"),
        },
    }


def _render_diff_report_html(data: Dict[str, Any]) -> str:
    """Render the diff report data as a self-contained HTML document."""
    import html as html_mod

    esc = html_mod.escape

    semver = data["semver"]
    policy = data["policy"]
    changes = data["changes"]
    old_health = data["health"]["old"]
    new_health = data["health"]["new"]

    # Bump color
    bump_colors = {
        "major": "#dc2626",
        "minor": "#2563eb",
        "patch": "#16a34a",
        "none": "#6b7280",
    }
    bump_val = semver["bump"].lower() if isinstance(semver["bump"], str) else "none"
    bump_color = bump_colors.get(bump_val, "#6b7280")

    # Gate color
    gate_colors = {"fail": "#dc2626", "warn": "#d97706", "pass": "#16a34a"}
    gate_color = gate_colors.get(policy["decision"], "#6b7280")

    # Build change rows
    change_rows = []
    for c in changes:
        severity_class = "breaking" if c["is_breaking"] else "non-breaking"
        severity_label = "Breaking" if c["is_breaking"] else "Compatible"
        severity_dot = "#dc2626" if c["is_breaking"] else "#16a34a"
        change_rows.append(
            f'<tr class="{severity_class}">'
            f'<td><span class="dot" style="background:{severity_dot}"></span> {esc(severity_label)}</td>'
            f"<td><code>{esc(c['type'])}</code></td>"
            f"<td><code>{esc(c['path'])}</code></td>"
            f"<td>{esc(c['message'])}</td>"
            f"</tr>"
        )
    change_rows_html = "\n".join(change_rows) if change_rows else '<tr><td colspan="4" class="empty">No changes detected</td></tr>'

    # Build violation rows
    violation_rows = []
    for v in policy["violations"]:
        sev_color = "#dc2626" if v["severity"] == "error" else "#d97706"
        violation_rows.append(
            f"<tr>"
            f'<td><span class="dot" style="background:{sev_color}"></span> {esc(v["severity"].upper())}</td>'
            f"<td><code>{esc(v['rule'])}</code></td>"
            f"<td>{esc(v['message'])}</td>"
            f"<td><code>{esc(v['path'])}</code></td>"
            f"</tr>"
        )
    violation_rows_html = "\n".join(violation_rows) if violation_rows else '<tr><td colspan="4" class="empty">No policy violations</td></tr>'

    # Health dimensions
    def _health_dimensions_html(health: Dict[str, Any], label: str) -> str:
        dims = health.get("dimensions", {})
        if not dims:
            return f"<p>{label}: No dimensions available</p>"
        rows = []
        for dim_name, dim_data in dims.items():
            score = dim_data.get("score", 0)
            bar_color = "#16a34a" if score >= 70 else "#d97706" if score >= 40 else "#dc2626"
            rows.append(
                f'<div class="health-row">'
                f'<span class="health-label">{esc(dim_name.replace("_", " ").title())}</span>'
                f'<div class="health-bar-bg"><div class="health-bar" style="width:{score}%;background:{bar_color}"></div></div>'
                f'<span class="health-score">{score}</span>'
                f"</div>"
            )
        return "\n".join(rows)

    old_dims_html = _health_dimensions_html(old_health, "Old")
    new_dims_html = _health_dimensions_html(new_health, "New")

    # Migration guide
    migration_html = ""
    if data.get("migration_guide"):
        lines = data["migration_guide"].split("\n")
        migration_parts = []
        in_pre = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_pre:
                    migration_parts.append("</pre>")
                    in_pre = False
                else:
                    migration_parts.append("<pre>")
                    in_pre = True
            elif in_pre:
                migration_parts.append(esc(line))
            elif stripped.startswith("# "):
                migration_parts.append(f"<h3>{esc(stripped[2:])}</h3>")
            elif stripped.startswith("## "):
                migration_parts.append(f"<h4>{esc(stripped[3:])}</h4>")
            elif stripped.startswith("### "):
                migration_parts.append(f"<h5>{esc(stripped[4:])}</h5>")
            elif stripped.startswith("- "):
                migration_parts.append(f"<li>{esc(stripped[2:])}</li>")
            elif stripped:
                migration_parts.append(f"<p>{esc(stripped)}</p>")
        if in_pre:
            migration_parts.append("</pre>")
        migration_html = f"""
        <section class="section">
            <h2>Migration Guide</h2>
            <div class="migration-content">
                {"".join(migration_parts)}
            </div>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API Diff Report -- {esc(data['old_version'])} to {esc(data['new_version'])}</title>
<style>
  :root {{
    --bg: #f8fafc;
    --surface: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --text-muted: #64748b;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --mono: "SF Mono", "Fira Code", "Fira Mono", Menlo, monospace;
    --radius: 8px;
    --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 2rem;
    max-width: 1100px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.15rem; font-weight: 600; margin-bottom: 1rem; color: var(--text); }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
    gap: 0.5rem;
  }}
  .header-meta {{ color: var(--text-muted); font-size: 0.85rem; }}
  .badge {{
    display: inline-block;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    color: #fff;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }}
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
    box-shadow: var(--shadow);
  }}
  .card-label {{ font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.4rem; }}
  .card-value {{ font-size: 1.75rem; font-weight: 700; }}
  .card-sub {{ font-size: 0.8rem; color: var(--text-muted); margin-top: 0.25rem; }}
  .section {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.5rem;
    margin-bottom: 1.5rem;
    box-shadow: var(--shadow);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
  }}
  th {{
    text-align: left;
    padding: 0.6rem 0.75rem;
    border-bottom: 2px solid var(--border);
    color: var(--text-muted);
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  td {{
    padding: 0.6rem 0.75rem;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr.breaking {{ background: #fef2f2; }}
  code {{
    font-family: var(--mono);
    font-size: 0.8rem;
    background: #f1f5f9;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
  }}
  .dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 0.4rem;
    vertical-align: middle;
  }}
  .empty {{
    text-align: center;
    color: var(--text-muted);
    padding: 1.5rem;
    font-style: italic;
  }}
  .health-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
  }}
  .health-col h3 {{
    font-size: 0.95rem;
    font-weight: 600;
    margin-bottom: 0.75rem;
  }}
  .health-row {{
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }}
  .health-label {{
    width: 120px;
    font-size: 0.8rem;
    color: var(--text-muted);
    flex-shrink: 0;
  }}
  .health-bar-bg {{
    flex: 1;
    height: 8px;
    background: #e2e8f0;
    border-radius: 4px;
    overflow: hidden;
  }}
  .health-bar {{
    height: 100%;
    border-radius: 4px;
    transition: width 0.3s ease;
  }}
  .health-score {{
    width: 30px;
    text-align: right;
    font-size: 0.8rem;
    font-weight: 600;
    flex-shrink: 0;
  }}
  .migration-content {{
    font-size: 0.9rem;
    line-height: 1.7;
  }}
  .migration-content h3 {{ font-size: 1.05rem; margin: 1rem 0 0.5rem; }}
  .migration-content h4 {{ font-size: 0.95rem; margin: 0.75rem 0 0.4rem; }}
  .migration-content h5 {{ font-size: 0.9rem; margin: 0.5rem 0 0.3rem; }}
  .migration-content li {{ margin-left: 1.5rem; margin-bottom: 0.25rem; }}
  .migration-content pre {{
    background: #1e293b;
    color: #e2e8f0;
    padding: 1rem;
    border-radius: var(--radius);
    overflow-x: auto;
    font-family: var(--mono);
    font-size: 0.8rem;
    margin: 0.5rem 0;
  }}
  .footer {{
    text-align: center;
    color: var(--text-muted);
    font-size: 0.75rem;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border);
  }}
  @media (max-width: 600px) {{
    body {{ padding: 1rem; }}
    .cards {{ grid-template-columns: 1fr 1fr; }}
    .health-grid {{ grid-template-columns: 1fr; }}
  }}
  @media print {{
    body {{ padding: 0; }}
    .section {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>{esc(data.get('new_title') or data.get('old_title') or 'API')} -- Diff Report</h1>
    <div class="header-meta">
      {esc(data['old_version'])} &rarr; {esc(data['new_version'])}
      &middot; Generated {esc(data['generated_at'][:19])} UTC
    </div>
  </div>
  <div>
    <span class="badge" style="background:{bump_color}">{esc(bump_val.upper())}</span>
    <span class="badge" style="background:{gate_color}">Gate: {esc(policy['decision'].upper())}</span>
  </div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Total Changes</div>
    <div class="card-value">{data['total_changes']}</div>
  </div>
  <div class="card">
    <div class="card-label">Breaking</div>
    <div class="card-value" style="color:#dc2626">{data['breaking_count']}</div>
  </div>
  <div class="card">
    <div class="card-label">Non-Breaking</div>
    <div class="card-value" style="color:#16a34a">{data['non_breaking_count']}</div>
  </div>
  <div class="card">
    <div class="card-label">Semver Bump</div>
    <div class="card-value" style="color:{bump_color}">{esc(bump_val.upper())}</div>
    <div class="card-sub">{semver['counts'].get('breaking', 0)} breaking, {semver['counts'].get('additive', 0)} additive, {semver['counts'].get('patch', 0)} patch</div>
  </div>
  <div class="card">
    <div class="card-label">Policy Gate</div>
    <div class="card-value" style="color:{gate_color}">{esc(policy['decision'].upper())}</div>
    <div class="card-sub">{policy['errors']} errors, {policy['warnings']} warnings</div>
  </div>
</div>

<section class="section">
  <h2>Changes</h2>
  <table>
    <thead>
      <tr>
        <th style="width:100px">Severity</th>
        <th style="width:160px">Type</th>
        <th style="width:220px">Path</th>
        <th>Description</th>
      </tr>
    </thead>
    <tbody>
      {change_rows_html}
    </tbody>
  </table>
</section>

<section class="section">
  <h2>Policy Violations</h2>
  <table>
    <thead>
      <tr>
        <th style="width:100px">Severity</th>
        <th style="width:160px">Rule</th>
        <th>Message</th>
        <th style="width:200px">Path</th>
      </tr>
    </thead>
    <tbody>
      {violation_rows_html}
    </tbody>
  </table>
</section>
{migration_html}
<section class="section">
  <h2>Spec Health</h2>
  <div class="health-grid">
    <div class="health-col">
      <h3>Baseline -- {esc(data['old_version'])} (Grade: {esc(str(old_health.get('grade', '?')))}  Score: {old_health.get('overall_score', '?')})</h3>
      {old_dims_html}
    </div>
    <div class="health-col">
      <h3>Proposed -- {esc(data['new_version'])} (Grade: {esc(str(new_health.get('grade', '?')))}  Score: {new_health.get('overall_score', '?')})</h3>
      {new_dims_html}
    </div>
  </div>
</section>

<div class="footer">
  Generated by Delimit -- API governance for teams that ship.
  &middot; <a href="https://delimit.ai" style="color:var(--text-muted)">delimit.ai</a>
</div>

</body>
</html>"""

    return html
