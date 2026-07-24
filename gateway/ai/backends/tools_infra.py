"""
Real implementations for infrastructure tools (replacing suite stubs).

Tools:
  - security_audit: dep audit + anti-pattern scan + secret detection
  - obs_status: system health (disk, memory, services, uptime)
  - obs_metrics: live system metrics from /proc
  - obs_logs: search system and application logs
  - release_plan: git-based release planning
  - release_status: file-based deploy tracker

All tools work WITHOUT external integrations by default.
Optional upgrades noted in each function's docstring.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.tools_infra")

# ─── Helpers ──────────────────────────────────────────────────────────────

DEPLOYS_DIR = Path(os.environ.get("DELIMIT_DEPLOYS_DIR", os.path.expanduser("~/.delimit/deploys")))

# Secret patterns: name -> regex
SECRET_PATTERNS = {
    "aws_access_key": r"(?:AKIA[0-9A-Z]{16})",
    "aws_secret_key": r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}",
    "generic_api_key": r"\b(?:api[_-]?key|apikey)\b\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}",
    "generic_secret": r"\b(?:secret|password|passwd|token)\b\s*[=:]\s*['\"]?[^\s'\"]{8,}",
    # Catches dict/JSON-style credentials where a key like password or api_key
    # is followed by a quoted literal value (>=4 chars). Example shape omitted
    # intentionally so the scanner does not flag this comment as a finding.
    "dict_credential": r"""['\"](?:password|passwd|secret|api_key|apikey|token|auth_token|access_token|private_key)['\"][\s]*:[\s]*['\"][^'\"]{4,}['\"]""",
    "private_key_header": r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----",
    "github_token": r"gh[pousr]_[A-Za-z0-9_]{36,}",
    "slack_token": r"xox[baprs]-[0-9A-Za-z\-]{10,}",
    "jwt_token": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
}

# False-positive exclusions for generic credential patterns — values that are
# clearly not real secrets (placeholders, env-var lookups, test fixtures,
# function-call RHS, demo literals, local variable assignments from parsers).
_CREDENTIAL_FALSE_POSITIVES = re.compile(
    r"(?:environ|getenv|process\.env|os\.environ|"
    r"<configured|example|placeholder|REDACTED|"
    r"your[_-]?(?:password|secret|token|key)|"
    r"change[_-]?me|TODO|FIXME|xxx+|\.{4,}|"
    r"\$\{|%\(|None|null|undefined|"
    r"test[_-]?(?:password|secret|token|key)|"
    # Test fixture patterns — fake keys like hosted-key-1, user-key-2, sk-test, gem-test
    r"hosted[_-]key[_-]?\d*|user[_-]key[_-]?\d*|"
    r"(?:codex|gem|grok)[_-]test|sk[_-]test|"
    r"bad[:\-]token|fake[_-]?(?:key|token|secret)|"
    # Demo/sample literal values used in docs, recordings, fixtures
    r"sk-ant-demo|sk-demo|AIza-demo|xai-demo|demo[_-]?(?:key|secret|token)|"
    r"-demo['\"]|"
    # Function-call RHS (reading from parsed JSON, env, getters, slicing strings)
    r"json\.loads|\.read_text\(|\.slice\(|\.split\(|"
    r"\w+\.get\(|token\s*=\s*_make_token|"
    # RHS that is a parameter reference like token=tokens.get("access_token"...
    r"=\s*\w+\.get\(|"
    # Dict index dereference: token_data["token"], result["secret"], etc.
    r"_data\[|_result\[|"
    # LED-1278 (b): function-call RHS with leading underscore (e.g. _load_token())
    r"=\s*_\w+\(|"
    # LED-1278 (c) [2026-05-22]: naked function-call RHS without leading
    # underscore. Matches the common shape `const token = readCurrentToken();`
    # in bin/delimit-cli.js — the token is being READ from somewhere, not
    # hardcoded. Tightened with `\s*;?\s*$` to require end-of-statement so
    # we don't suppress `token = realLeak("AKIAIOSFODNN7EXAMPLE")` shapes
    # where the call argument is itself a literal secret.
    r"=\s*\w+\([^)]{0,40}\)\s*;?\s*$|"
    # LED-1278 (c) [2026-05-22]: parenthesized property-access fallback chain
    # like `const token = (options.token || process.env.TOKEN)`. Common shape
    # for CLI option parsing where the RHS reads from a known input source,
    # never a literal. Requires the open-paren to be followed by a word + dot
    # (property access) so we don't match `token = ("AKIA..." || "")` shapes.
    r"=\s*\(\s*\w+\.\w+|"
    # LED-1278 (b): documentation/example placeholders in angle brackets
    r"<[^>]*?(?:long|same|random|your|placeholder|example|secret|token|key)[^>]*?>|"
    # Bare `if not <var>:` and similar control-flow lines that mention
    # the credential variable name but contain no value.
    r"if\s+not\s+\w+:|"
    # Python control-flow block-opener: a colon immediately followed by
    # a newline (no quoted value on the same line). Such a colon is an
    # if/while/def/class block-opener, not a key-value separator.
    r":\s*\n)",
    re.IGNORECASE,
)

# Dangerous code patterns: name -> (regex, description, severity)
ANTI_PATTERNS = {
    "eval_usage": (r"\beval\s*\(", "Use of eval() — potential code injection", "high"),  # nosec B-eval_usage: regex-pattern DEFINITION string (not runtime eval)
    "exec_usage": (r"\bexec\s*\(", "Use of exec() — potential code injection", "high"),  # nosec B-exec_usage: regex-pattern DEFINITION string (not runtime exec)
    "sql_concat": (r"""(?:execute|cursor\.execute|query)\s*\(\s*(?:f['\"]|['\"].*%s|.*\+\s*['\"])""", "SQL string concatenation — potential SQL injection", "critical"),
    "dangerous_innerHTML": (r"dangerouslySetInnerHTML", "dangerouslySetInnerHTML — potential XSS", "high"),  # nosec B-dangerous_innerHTML: regex-pattern DEFINITION string
    "subprocess_shell": (r"subprocess\.\w+\([^)]*shell\s*=\s*True", "subprocess with shell=True — potential command injection", "medium"),
    "pickle_load": (r"pickle\.loads?\(", "pickle.load — potential arbitrary code execution", "high"),
    "yaml_unsafe_load": (r"yaml\.load\([^)]*(?!Loader)", "yaml.load without safe Loader", "medium"),
    "hardcoded_ip": (r"\b(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b", "Hardcoded internal IP address", "low"),
}

# File extensions to scan
SCAN_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".java", ".rs", ".yaml", ".yml", ".json", ".env", ".sh", ".bash"}

# Skip directories
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".tox", "dist", "build", ".next", ".nuxt", "vendor"}

# LED-1680/3008: default repo/root scans must not recurse through local
# assistant state stores. Those dirs routinely contain credentials, historical
# backups, chat transcripts, and plugin caches; scanning them as source creates
# noisy governance tasks and can leak secret snippets into audit output. Explicit
# scans of one of these directories, or of a single file inside one, still work.
OPERATIONAL_STATE_DIRS = {
    ".delimit",
    ".claude",
    ".codex",
    ".gemini",
    ".agents",
    ".config",
    ".local",
    ".cache",
    ".cloudflared",
}
OPERATIONAL_STATE_FILES = {".delimit.json", ".delimit-mcp.json"}
# Google service-account key files sometimes sit in the operator home root.
# Skip this filename shape only for broad home scans; repo scans still catch it.
HOME_OPERATIONAL_FILE_PATTERNS = (
    re.compile(r"^[a-z][a-z0-9-]+-[0-9a-f]{12}\.json$", re.IGNORECASE),
)


def _is_home_operational_file(filename: str, root: Path) -> bool:
    return root == Path.home().resolve() and any(
        pattern.match(filename) for pattern in HOME_OPERATIONAL_FILE_PATTERNS
    )


# LED-1278 (a): test-tree path patterns excluded by default. The scanner walks  # nosec
# test directories with prod rules, so test fixtures (placeholder tokens,  # nosec
# trivial JWT bodies, code-injection demos) get surfaced as critical findings  # nosec
# on every audit. Default behavior now skips these; callers can pass  # nosec
# include_tests=True to scan everything.  # nosec
TEST_PATH_PATTERNS = (
    re.compile(r"(?:^|[\\/])tests?[\\/]"),         # tests/ or test/ as a path component
    re.compile(r"(?:^|[\\/])__tests__[\\/]"),      # JS __tests__/
    re.compile(r"(?:^|[\\/])spec[\\/]"),           # spec/
    re.compile(r"(?:^|[\\/])fixtures?[\\/]"),      # fixtures/ or fixture/
    re.compile(r"(?:^|[\\/])test_[^\\/]+\.py$"),   # test_*.py
    re.compile(r"_test\.(?:py|go|rb|java)$"),       # *_test.py / *_test.go
    re.compile(r"\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)$"),  # *.test.js, *.spec.tsx
)


def _is_test_path(path: str) -> bool:
    """Return True if path looks like a test file/dir per TEST_PATH_PATTERNS."""
    s = str(path)
    return any(pat.search(s) for pat in TEST_PATH_PATTERNS)


# LED-1278 (b): well-known dummy / fixture values. Even when include_tests=True
# (or when production code intentionally embeds canonical placeholders in
# docs/examples), these specific shapes should be suppressed as `info` log
# lines, not raised as critical findings.
#
# Each entry: (regex applied to the matched secret text, human label).
KNOWN_DUMMY_PATTERNS = [
    # AWS canonical dummy from official AWS documentation.
    (re.compile(r"AKIAIOSFODNN7EXAMPLE"), "aws_doc_dummy"),
    # GitHub token placeholders that use the printable-alphabet pattern.
    (re.compile(r"^gh[pousr]_ABCDEFGHIJKLMNOPQRSTUVWXYZ", re.IGNORECASE), "github_alphabet_dummy"),
    # Slack tokens with the leading 1234567890 sequence.
    (re.compile(r"^xox[baprs]-1234567890-"), "slack_seq_dummy"),
    # JWT with the unsigned-HS256 header + trivial body. We match the literal
    # eyJhbGciOiJIUzI1NiJ9 header and check the payload separately below.
    (re.compile(r"^eyJhbGciOiJIUzI1NiJ9\."), "jwt_hs256_trivial"),
    # Generic dict-credential placeholder values: fake/test/dummy/example/etc.
    (re.compile(r"['\"](?:fake|test|dummy|example|placeholder|stale|from-)[A-Za-z0-9_\-]*['\"]\s*$", re.IGNORECASE),
     "generic_placeholder_value"),
    # Common documentation placeholder: token = "YOUR_DISCORD_BOT_TOKEN".
    (re.compile(r"YOUR_[A-Z0-9_]*(?:TOKEN|SECRET|KEY)", re.IGNORECASE),
     "your_token_placeholder"),
    # Provider test-key shapes: xai-key-123, google-key-7, claude-key-2 etc.
    (re.compile(r"['\"](?:xai|google|claude|gem|grok|codex|ollama)[-_]?key[-_]?\d+['\"]\s*$", re.IGNORECASE),
     "provider_test_key"),
]


# LED-2278 [2026-05-27]: positive value-shape gate for generic_secret.
#
# The generic_secret regex (`\b(?:secret|password|passwd|token)\b\s*[=:]\s*
# ['\"]?[^\s'\"]{8,}`) fires on ANY assignment/key whose trigger word is
# followed by 8+ non-space chars — including ordinary code where the RHS is
# an identifier, a function call, or a subscript expression, not a hardcoded
# literal. Examples that recurrently false-positive in this very repo:
#
#     token = self._unescape_json_pointer_token(raw_token)   # method call
#     scheme, token = parts[0].strip().lower(), parts[1]     # tuple/subscript
#
# The pre-existing `_CREDENTIAL_FALSE_POSITIVES` negative list is whack-a-mole
# (one alternation per observed shape). This positive gate inverts the logic:
# a `generic_secret` hit is only credible when the VALUE is a *quoted string
# literal* with secret-like entropy/length. If the value is an unquoted
# identifier / call / expression, it is code, not a leaked secret — suppress.
#
# Conservative by construction: this gate only ever SUPPRESSES generic_secret
# hits whose value is non-literal. It never suppresses a quoted literal, so
# real hardcoded secrets (and all the existing detection tests) still fire.
# Applies to generic_secret only — aws_secret_key / github_token / etc. keep
# their own format-specific regexes untouched.

# A value (after the = or :) that begins with a quote is a string literal.
_GENERIC_SECRET_VALUE_RE = re.compile(
    r"""\b(?:secret|password|passwd|token)\b\s*[=:]\s*(?P<q>['\"])(?P<val>[^'\"]*)"""
)


def _generic_secret_value_is_literal(matched_text: str) -> bool:
    """True only if the generic_secret match assigns a *quoted string literal*.

    The generic_secret regex tolerates an optional opening quote, so it also
    matches `token = some_call()` (unquoted RHS). A real hardcoded secret is a
    quoted literal with entropy; an unquoted RHS is an identifier/expression
    (variable ref, function call, subscript, attribute access) and is code, not
    a leak. Return False for the unquoted/expression case so the caller can
    suppress it, True for a credible quoted-literal value.
    """
    m = _GENERIC_SECRET_VALUE_RE.search(matched_text)
    if not m:
        # No opening quote captured → RHS is a bare identifier / expression
        # (e.g. `token = self._make(...)`, `scheme, token = parts[0]`). Not a
        # hardcoded literal; suppress.
        return False
    val = m.group("val")
    # A quoted literal with too little content is not secret-shaped. The outer
    # regex already required 8+ chars total, but the quote may sit mid-match;
    # require the literal body itself to be reasonably long.
    if len(val) < 6:
        return False
    # Pure-identifier literals inside quotes (e.g. a quoted dict KEY like
    # "access_token") that are all word chars + separators and read like an
    # English/identifier token rather than a high-entropy secret: require at
    # least some character-class mixing OR sufficient length to look secret-y.
    has_lower = any(c.islower() for c in val)
    has_upper = any(c.isupper() for c in val)
    has_digit = any(c.isdigit() for c in val)
    # Treat underscore/hyphen as word chars (not entropy): a quoted
    # identifier-shaped value like "access_token" should NOT count as a
    # multi-class high-entropy secret on the strength of its separators alone.
    has_symbol = any(not c.isalnum() and c not in (" ", "_", "-") for c in val)
    classes = sum([has_lower, has_upper, has_digit, has_symbol])
    # Credible secret: multi-class entropy, OR a long single-class blob.
    return classes >= 2 or len(val) >= 16


def _looks_like_known_dummy(secret_name: str, matched_text: str) -> Optional[str]:
    """Return a label if matched_text is a known-dummy/fixture value, else None.

    Used by the secret scanner to convert what would otherwise be a critical
    finding into an `info`-level suppressed entry. Keeps the audit-trail
    visible (so a future regression in the allowlist is detectable) while
    eliminating the false-positive-storm noise.

    For JWT, additionally checks that the body is the trivial `sub:1234567890`
    payload — we don't want to suppress real signed JWTs that happen to use
    HS256.
    """
    for pattern, label in KNOWN_DUMMY_PATTERNS:
        if pattern.search(matched_text):
            if label == "jwt_hs256_trivial":
                # Only treat as dummy if the payload is the canonical demo
                # body (`sub: "1234567890"` or trivial abc123 segment).
                # The JWT pattern produces something like:
                #   eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi789
                # The middle segment base64-decodes to {"sub":"1234567890"}.
                if (
                    "eyJzdWIiOiIxMjM0NTY3ODkwIn0" in matched_text
                    or re.search(r"\.[A-Za-z0-9_-]*abc123[A-Za-z0-9_-]*$", matched_text)
                ):
                    return label
                continue
            return label
    return None


def _run_cmd(cmd: List[str], timeout: int = 30, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Run a command and return stdout, stderr, returncode.

    Security: always uses list-form args (never shell=True).
    Validates cwd if provided and rejects null bytes in arguments.
    """
    # Defense-in-depth: reject null bytes in any argument
    for i, arg in enumerate(cmd):
        if "\x00" in str(arg):
            return {"stdout": "", "stderr": f"Argument {i} contains null bytes", "returncode": -4}
    if cwd and "\x00" in cwd:
        return {"stdout": "", "stderr": "cwd contains null bytes", "returncode": -4}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"Command not found: {cmd[0]}", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Command timed out after {timeout}s", "returncode": -2}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -3}


def _bump_semver(version: str, bump: str) -> str:
    """Compute the next semver version without mutating files."""
    try:
        major_s, minor_s, patch_s = version.split(".", 2)
        major = int(major_s)
        minor = int(minor_s)
        patch = int(patch_s)
    except Exception as exc:
        raise ValueError(f"Invalid semver version '{version}'") from exc

    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor += 1
        patch = 0
    elif bump == "major":
        major += 1
        minor = 0
        patch = 0
    return f"{major}.{minor}.{patch}"


def _scan_files(target: str, include_tests: bool = False) -> List[Path]:
    """Collect scannable source files under target.

    LED-1278 (a): when include_tests=False (the new default), skip files that
    match TEST_PATH_PATTERNS so test fixtures do not surface as findings.
    Single-file targets are always scanned regardless (caller asked explicitly).
    """
    root = Path(target).resolve()
    files = []
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    skip_operational_state = root.name not in OPERATIONAL_STATE_DIRS
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _err: None):
        skipped_dirs = set(SKIP_DIRS)
        if skip_operational_state:
            skipped_dirs.update(OPERATIONAL_STATE_DIRS)
        dirnames[:] = [d for d in dirnames if d not in skipped_dirs]
        if not include_tests:
            # Prune obvious test directory names before recursing so we don't
            # walk huge __tests__/ trees just to discard them later.
            dirnames[:] = [
                d for d in dirnames
                if d not in ("tests", "test", "__tests__", "spec", "fixtures", "fixture")
            ]
        for filename in filenames:
            if skip_operational_state and filename in OPERATIONAL_STATE_FILES:
                continue
            if skip_operational_state and _is_home_operational_file(filename, root):
                continue
            p = Path(dirpath) / filename
            if p.suffix not in SCAN_EXTENSIONS:
                continue
            if not include_tests:
                try:
                    rel = str(p.relative_to(root))
                except ValueError:
                    rel = str(p)
                if _is_test_path(rel):
                    continue
            files.append(p)
            # Cap to avoid scanning massive repos
            if len(files) >= 5000:
                return files
    return files


# ─── 5. security_audit ──────────────────────────────────────────────────

def security_audit(target: str = ".", include_tests: bool = False) -> Dict[str, Any]:
    """Audit security: dependency vulnerabilities + anti-patterns + secret detection.

    Default: runs pip-audit/npm-audit, regex scans for secrets and dangerous patterns.
    Optional upgrade: set SNYK_TOKEN or TRIVY_PATH for enhanced scanning.

    LED-1278 fixes:
      (a) include_tests defaults to False — test directories (tests/, __tests__/,
          spec/, fixtures/, *_test.py, *.test.tsx, etc.) are skipped so
          test fixtures don't get raised as critical production findings.
          Pass include_tests=True to scan everything (legacy behavior).
      (b) Well-known dummy/placeholder values (AWS canonical example,
          alphabet-pattern GitHub tokens, leading-1234567890 Slack tokens,
          trivial JWT, fake/test/dummy/placeholder dict values, provider
          test-key shapes) are suppressed and recorded as `info`-severity
          allowlist hits in `suppressed_findings` for audit visibility.

    Args:
        target: Repository or file path to audit.
        include_tests: When True, scan test directories (default False).
    """
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    vulnerabilities = []
    anti_patterns_found = []
    secrets_found = []
    suppressed_findings: List[Dict[str, Any]] = []  # LED-1278 (b): allowlist log
    tools_used = []
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}

    # --- 1. Dependency audit ---
    cwd = str(target_path) if target_path.is_dir() else str(target_path.parent)

    # Python: pip-audit
    if (target_path / "requirements.txt").exists() or (target_path / "pyproject.toml").exists() or (target_path / "setup.py").exists():
        pip_audit = shutil.which("pip-audit")
        if pip_audit:
            r = _run_cmd([pip_audit, "--format", "json", "--desc"], timeout=60, cwd=cwd)
            tools_used.append("pip-audit")
            if r["returncode"] == 0 or r["stdout"].strip():
                try:
                    entries = json.loads(r["stdout"]) if r["stdout"].strip() else []
                    # pip-audit returns {"dependencies": [...]}
                    deps = entries if isinstance(entries, list) else entries.get("dependencies", [])
                    for dep in deps:
                        for vuln in dep.get("vulns", []):
                            sev = vuln.get("fix_versions", ["unknown"])
                            vulnerabilities.append({
                                "source": "pip-audit",
                                "package": dep.get("name", "unknown"),
                                "installed": dep.get("version", "unknown"),
                                "id": vuln.get("id", "unknown"),
                                "description": vuln.get("description", "")[:200],
                                "fix_versions": vuln.get("fix_versions", []),
                                "severity": "high",
                            })
                            severity_counts["high"] += 1
                except (json.JSONDecodeError, KeyError):
                    pass
        else:
            tools_used.append("pip-audit (not installed)")

    # Node: npm audit
    if (target_path / "package.json").exists():
        npm = shutil.which("npm")
        if npm:
            r = _run_cmd([npm, "audit", "--json"], timeout=60, cwd=cwd)
            tools_used.append("npm-audit")
            try:
                data = json.loads(r["stdout"]) if r["stdout"].strip() else {}
                advisories = data.get("vulnerabilities", data.get("advisories", {}))
                if isinstance(advisories, dict):
                    for name, info in advisories.items():
                        sev = info.get("severity", "high") if isinstance(info, dict) else "high"
                        vulnerabilities.append({
                            "source": "npm-audit",
                            "package": name,
                            "severity": sev,
                            "title": info.get("title", "") if isinstance(info, dict) else "",
                            "via": str(info.get("via", ""))[:200] if isinstance(info, dict) else "",
                        })
                        sev_key = sev if sev in severity_counts else "high"
                        severity_counts[sev_key] += 1
            except (json.JSONDecodeError, KeyError):
                pass
        else:
            tools_used.append("npm (not installed)")

    # Optional: Snyk
    snyk_token = os.environ.get("SNYK_TOKEN")
    if snyk_token and shutil.which("snyk"):
        r = _run_cmd(["snyk", "test", "--json"], timeout=120, cwd=cwd)
        tools_used.append("snyk")
        try:
            data = json.loads(r["stdout"]) if r["stdout"].strip() else {}
            for vuln in data.get("vulnerabilities", []):
                vulnerabilities.append({
                    "source": "snyk",
                    "package": vuln.get("packageName", "unknown"),
                    "severity": vuln.get("severity", "high"),
                    "id": vuln.get("id", ""),
                    "title": vuln.get("title", ""),
                })
                sev = vuln.get("severity", "high")
                sev_key = sev if sev in severity_counts else "high"
                severity_counts[sev_key] += 1
        except (json.JSONDecodeError, KeyError):
            pass

    # Optional: Trivy
    trivy_path = os.environ.get("TRIVY_PATH") or shutil.which("trivy")
    if trivy_path and os.path.isfile(trivy_path):
        r = _run_cmd([trivy_path, "fs", "--format", "json", str(target_path)], timeout=120)
        tools_used.append("trivy")
        try:
            data = json.loads(r["stdout"]) if r["stdout"].strip() else {}
            for result_entry in data.get("Results", []):
                for vuln in result_entry.get("Vulnerabilities", []):
                    vulnerabilities.append({
                        "source": "trivy",
                        "package": vuln.get("PkgName", "unknown"),
                        "severity": vuln.get("Severity", "UNKNOWN").lower(),
                        "id": vuln.get("VulnerabilityID", ""),
                        "title": vuln.get("Title", ""),
                    })
                    sev = vuln.get("Severity", "high").lower()
                    sev_key = sev if sev in severity_counts else "high"
                    severity_counts[sev_key] += 1
        except (json.JSONDecodeError, KeyError):
            pass

    # --- 2. Anti-pattern scan ---
    files = _scan_files(target, include_tests=include_tests)
    scan_label = f"pattern-scanner ({len(files)} files"
    scan_label += ", include_tests=True" if include_tests else ", tests excluded"
    tools_used.append(scan_label + ")")

    for fpath in files:
        try:
            content = fpath.read_text(errors="ignore")
        except (OSError, PermissionError):
            continue

        rel = str(fpath.relative_to(Path(target).resolve())) if Path(target).resolve() in fpath.parents or fpath == Path(target).resolve() else str(fpath)

        # Secret detection
        # Patterns where false-positive filtering applies (generic/dict patterns only)
        _FP_FILTERED = {"generic_secret", "dict_credential", "generic_api_key"}
        for secret_name, pattern in SECRET_PATTERNS.items():
            for match in re.finditer(pattern, content):
                matched_text = match.group(0)
                # Skip false positives only for generic patterns (not specific token formats)
                if secret_name in _FP_FILTERED and _CREDENTIAL_FALSE_POSITIVES.search(matched_text):
                    continue
                # LED-2278: positive value-shape gate for generic_secret. Only
                # flag when the assigned value is a quoted string literal with
                # secret-like entropy; an unquoted identifier/call/expression
                # RHS (`token = self._make(...)`, `scheme, token = parts[0]`)
                # is code, not a leaked secret. Conservative: never suppresses
                # a quoted literal, so real hardcoded secrets still fire.
                if secret_name == "generic_secret" and not _generic_secret_value_is_literal(matched_text):
                    continue
                # LED-2278: the scanner's own source embeds the trigger words in
                # regex/doc comments (e.g. the `token = realLeak(...)` example in
                # this module). Those are pattern DEFINITIONS, not secrets.
                if secret_name == "generic_secret" and rel.endswith("ai/backends/tools_infra.py"):
                    continue
                line_num = content[:match.start()].count("\n") + 1
                # LED-1278 (b): well-known dummy/placeholder values get
                # suppressed to info-level rather than raised as critical.
                # Logged in suppressed_findings so a future regression in the
                # allowlist (e.g. real key matching by accident) is auditable.
                dummy_label = _looks_like_known_dummy(secret_name, matched_text)
                if dummy_label:
                    suppressed_findings.append({
                        "file": rel,
                        "line": line_num,
                        "type": secret_name,
                        "reason": dummy_label,
                        "severity": "info",
                    })
                    severity_counts["info"] += 1
                    logger.info(
                        "security_audit: suppressed known-dummy %s (%s) in %s:%d",
                        secret_name, dummy_label, rel, line_num,
                    )
                    continue
                # Redact actual secret values in snippet output
                snippet_raw = content[max(0, match.start() - 10):match.end() + 10].strip()[:80]
                secrets_found.append({
                    "file": rel,
                    "line": line_num,
                    "type": secret_name,
                    "severity": "critical",
                    "snippet": snippet_raw,
                })
                severity_counts["critical"] += 1

        # Anti-pattern detection with industry-standard suppression markers.
        # Skip a match if the matched line contains `# nosec`, `// nosec`,
        # `# delimit:nosec`, or `// delimit:nosec` (anywhere on that line).
        # This matches bandit's convention for Python and is widely understood.
        content_lines = content.splitlines()
        for ap_name, (pattern, desc, sev) in ANTI_PATTERNS.items():
            for match in re.finditer(pattern, content):
                line_num = content[:match.start()].count("\n") + 1
                line_text = content_lines[line_num - 1] if 0 < line_num <= len(content_lines) else ""
                if re.search(r"(#|//)\s*(delimit:)?nosec\b", line_text):
                    continue
                anti_patterns_found.append({
                    "file": rel,
                    "line": line_num,
                    "pattern": ap_name,
                    "description": desc,
                    "severity": sev,
                })
                severity_counts[sev] += 1

    # --- 3. Check for .env in git ---
    env_in_git = False
    if (Path(target).resolve() / ".git").is_dir():
        r = _run_cmd(["git", "ls-files", "--cached", ".env"], cwd=cwd)
        if r["stdout"].strip():
            env_in_git = True
            anti_patterns_found.append({
                "file": ".env",
                "line": 0,
                "pattern": "env_in_git",
                "description": ".env file is tracked in git — secrets may be exposed in history",
                "severity": "critical",
            })
            severity_counts["critical"] += 1

    return {
        "target": str(target_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vulnerabilities": vulnerabilities,
        "anti_patterns": anti_patterns_found,
        "secrets_detected": len(secrets_found),
        "secrets": secrets_found[:20],  # Cap output to avoid huge responses
        "suppressed_findings": suppressed_findings[:20],  # LED-1278 (b): allowlist audit log
        "suppressed_count": len(suppressed_findings),
        "include_tests": include_tests,  # LED-1278 (a): expose scan scope
        "operational_state_excluded": target_path.name not in OPERATIONAL_STATE_DIRS,
        "env_in_git": env_in_git,
        "severity_summary": severity_counts,
        "tools_used": tools_used,
        "files_scanned": len(files),
        "total_findings": len(vulnerabilities) + len(anti_patterns_found) + len(secrets_found),
    }


# ─── 6. obs_status ──────────────────────────────────────────────────────

# Common service ports to probe
KNOWN_PORTS = {
    3000: "Node/Next.js",
    3001: "Dev server",
    4000: "GraphQL",
    5000: "Flask/FastAPI",
    5173: "Vite",
    5432: "PostgreSQL",
    6379: "Redis",
    8000: "Django/FastAPI",
    8080: "HTTP alt",
    8443: "HTTPS alt",
    9090: "Prometheus",
    9200: "Elasticsearch",
    27017: "MongoDB",
}


def obs_status() -> Dict[str, Any]:
    """System health: disk, memory, services, uptime. Uses system commands only."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "disk_usage": {},
        "memory_usage": {},
        "services_detected": [],
        "uptime": "",
        "process_count": 0,
        "load_average": [],
    }

    # Disk space
    r = _run_cmd(["df", "-h", "--output=target,size,used,avail,pcent", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay"])
    if r["returncode"] == 0:
        lines = r["stdout"].strip().split("\n")
        disks = []
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 5:
                disks.append({
                    "mount": parts[0],
                    "size": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "percent": parts[4],
                })
        result["disk_usage"] = disks

    # Memory
    r = _run_cmd(["free", "-m"])
    if r["returncode"] == 0:
        lines = r["stdout"].strip().split("\n")
        for line in lines:
            if line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 7:
                    total = int(parts[1])
                    used = int(parts[2])
                    result["memory_usage"] = {
                        "total_mb": total,
                        "used_mb": used,
                        "free_mb": int(parts[3]),
                        "available_mb": int(parts[6]) if len(parts) > 6 else total - used,
                        "percent_used": round(used / total * 100, 1) if total > 0 else 0,
                    }
            elif line.startswith("Swap:"):
                parts = line.split()
                if len(parts) >= 3:
                    result["swap_usage"] = {
                        "total_mb": int(parts[1]),
                        "used_mb": int(parts[2]),
                        "free_mb": int(parts[3]) if len(parts) > 3 else 0,
                    }

    # Uptime
    r = _run_cmd(["uptime", "-p"])
    if r["returncode"] == 0:
        result["uptime"] = r["stdout"].strip()
    else:
        # Fallback: read from /proc/uptime
        try:
            raw = Path("/proc/uptime").read_text().split()[0]
            secs = float(raw)
            days = int(secs // 86400)
            hours = int((secs % 86400) // 3600)
            mins = int((secs % 3600) // 60)
            result["uptime"] = f"up {days} days, {hours} hours, {mins} minutes"
        except Exception:
            result["uptime"] = "unknown"

    # Process count
    r = _run_cmd(["ps", "aux", "--no-headers"])
    if r["returncode"] == 0:
        result["process_count"] = len(r["stdout"].strip().split("\n"))

    # Load average
    try:
        loadavg = Path("/proc/loadavg").read_text().split()[:3]
        result["load_average"] = [float(x) for x in loadavg]
    except Exception:
        pass

    # Service detection via port probing
    services = []
    curl = shutil.which("curl")
    for port, name in KNOWN_PORTS.items():
        if curl:
            r = _run_cmd([curl, "-s", "-o", "/dev/null", "-w", "%{http_code}", "--connect-timeout", "1", f"http://localhost:{port}/"])
            if r["returncode"] == 0 and r["stdout"].strip() not in ("000", ""):
                services.append({"port": port, "name": name, "status": "up", "http_code": r["stdout"].strip()})
        else:
            # Fallback: check if port is listening via /proc/net/tcp
            try:
                hex_port = f"{port:04X}"
                tcp_data = Path("/proc/net/tcp").read_text()
                if hex_port in tcp_data:
                    services.append({"port": port, "name": name, "status": "listening"})
            except Exception:
                pass
    result["services_detected"] = services

    return result


# ─── 7. obs_metrics ─────────────────────────────────────────────────────

def obs_metrics(query: str = "system", time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    """Live system metrics from /proc. Query: cpu|memory|disk|io|all.

    Optional upgrade: set PROMETHEUS_URL or GRAFANA_URL for remote metrics.
    """
    result = {
        "query": query,
        "time_range": time_range,
        "source": source or "local",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": {},
    }

    # Check for Prometheus/Grafana integration
    prometheus_url = os.environ.get("PROMETHEUS_URL")
    if prometheus_url and source in ("prometheus", None):
        try:
            import urllib.request
            url = f"{prometheus_url}/api/v1/query?query={query}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                result["metrics"]["prometheus"] = data.get("data", {}).get("result", [])
                result["source"] = "prometheus"
                return result
        except Exception as e:
            result["metrics"]["prometheus_error"] = str(e)

    q = query.lower()

    # CPU metrics
    if q in ("cpu", "system", "all"):
        try:
            stat1 = Path("/proc/stat").read_text().split("\n")[0].split()[1:]
            time.sleep(0.2)
            stat2 = Path("/proc/stat").read_text().split("\n")[0].split()[1:]
            vals1 = [int(x) for x in stat1[:7]]
            vals2 = [int(x) for x in stat2[:7]]
            delta = [b - a for a, b in zip(vals1, vals2)]
            total = sum(delta)
            idle = delta[3]
            cpu_pct = round((total - idle) / total * 100, 1) if total > 0 else 0.0
            result["metrics"]["cpu_percent"] = cpu_pct
            result["metrics"]["cpu_cores"] = os.cpu_count()
        except Exception as e:
            result["metrics"]["cpu_error"] = str(e)

    # Memory metrics
    if q in ("memory", "mem", "system", "all"):
        try:
            meminfo = {}
            for line in Path("/proc/meminfo").read_text().split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    meminfo[key.strip()] = int(val.strip().split()[0])  # kB
            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            used = total - available
            result["metrics"]["memory_total_mb"] = round(total / 1024, 1)
            result["metrics"]["memory_used_mb"] = round(used / 1024, 1)
            result["metrics"]["memory_available_mb"] = round(available / 1024, 1)
            result["metrics"]["memory_percent"] = round(used / total * 100, 1) if total > 0 else 0
        except Exception as e:
            result["metrics"]["memory_error"] = str(e)

    # Disk I/O
    if q in ("disk", "io", "system", "all"):
        try:
            diskstats = Path("/proc/diskstats").read_text().split("\n")
            disks = []
            for line in diskstats:
                parts = line.split()
                if len(parts) >= 14:
                    dev = parts[2]
                    # Filter to real block devices (sda, nvme, vda, etc.)
                    if re.match(r'^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+)$', dev):
                        disks.append({
                            "device": dev,
                            "reads_completed": int(parts[3]),
                            "writes_completed": int(parts[7]),
                            "read_sectors": int(parts[5]),
                            "write_sectors": int(parts[9]),
                            "io_in_progress": int(parts[11]),
                        })
            result["metrics"]["disk_io"] = disks
        except Exception as e:
            result["metrics"]["disk_io_error"] = str(e)

        # Disk space
        r = _run_cmd(["df", "-B1", "--output=target,size,used,avail", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay"])
        if r["returncode"] == 0:
            lines = r["stdout"].strip().split("\n")[1:]
            disk_space = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        total_b = int(parts[1])
                        used_b = int(parts[2])
                        disk_space.append({
                            "mount": parts[0],
                            "total_gb": round(total_b / (1024**3), 1),
                            "used_gb": round(used_b / (1024**3), 1),
                            "percent": round(used_b / total_b * 100, 1) if total_b > 0 else 0,
                        })
                    except ValueError:
                        pass
            result["metrics"]["disk_space"] = disk_space

    # Network (brief)
    if q in ("network", "net", "all"):
        try:
            net_lines = Path("/proc/net/dev").read_text().split("\n")[2:]
            interfaces = []
            for line in net_lines:
                if ":" in line:
                    parts = line.split(":")
                    iface = parts[0].strip()
                    if iface in ("lo",):
                        continue
                    vals = parts[1].split()
                    if len(vals) >= 10:
                        interfaces.append({
                            "interface": iface,
                            "rx_bytes": int(vals[0]),
                            "tx_bytes": int(vals[8]),
                            "rx_packets": int(vals[1]),
                            "tx_packets": int(vals[9]),
                        })
            result["metrics"]["network"] = interfaces
        except Exception as e:
            result["metrics"]["network_error"] = str(e)

    return result


# ─── 8. obs_logs ─────────────────────────────────────────────────────────

# Default log paths to search
DEFAULT_LOG_PATHS = [
    "/var/log/syslog",
    "/var/log/messages",
    "/var/log/auth.log",
    "/var/log/kern.log",
    "/var/log/nginx/access.log",
    "/var/log/nginx/error.log",
    "/var/log/caddy/access.log",
]


def obs_logs(query: str, time_range: str = "1h", source: Optional[str] = None) -> Dict[str, Any]:
    """Search system and application logs.

    Optional upgrade: set ELASTICSEARCH_URL or LOKI_URL for centralized log search.
    """
    result = {
        "query": query,
        "time_range": time_range,
        "source": source or "local",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "matches": [],
        "total_lines_searched": 0,
        "sources_checked": [],
    }

    # Check for Elasticsearch integration
    es_url = os.environ.get("ELASTICSEARCH_URL")
    if es_url and source in ("elasticsearch", "es", None):
        try:
            import urllib.request
            url = f"{es_url}/_search"
            payload = json.dumps({"query": {"match": {"message": query}}, "size": 50}).encode()
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                hits = data.get("hits", {}).get("hits", [])
                result["matches"] = [{"source": "elasticsearch", "message": h["_source"].get("message", "")} for h in hits[:50]]
                result["source"] = "elasticsearch"
                return result
        except Exception as e:
            result["sources_checked"].append({"source": "elasticsearch", "error": str(e)})

    # Parse time_range to seconds for journalctl
    time_map = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "6h": 21600, "12h": 43200, "24h": 86400, "1d": 86400, "7d": 604800}
    since_secs = time_map.get(time_range, 3600)
    since_arg = f"--since=-{since_secs}s"

    # 1. journalctl (best on systemd systems)
    journalctl = shutil.which("journalctl")
    if journalctl:
        r = _run_cmd([journalctl, "--no-pager", "-g", query, since_arg, "--lines=100"], timeout=15)
        result["sources_checked"].append({"source": "journalctl", "available": True})
        if r["returncode"] in (0, 1):  # 1 = no matches
            lines = r["stdout"].strip().split("\n") if r["stdout"].strip() else []
            for line in lines[-50:]:  # Last 50 matches
                if line.strip():
                    result["matches"].append({"source": "journalctl", "line": line.strip()})
            result["total_lines_searched"] += len(lines)
    else:
        result["sources_checked"].append({"source": "journalctl", "available": False})

    # 2. Log file search
    log_paths = DEFAULT_LOG_PATHS[:]
    if source and source not in ("local", "journalctl", "elasticsearch", "es", "loki"):
        # Treat source as a custom log path
        log_paths = [source]

    for log_path in log_paths:
        p = Path(log_path)
        if not p.exists() or not p.is_file():
            continue
        result["sources_checked"].append({"source": log_path, "available": True})
        try:
            grep = shutil.which("grep")
            if grep:
                r = _run_cmd([grep, "-i", "-n", "--text", query, log_path], timeout=10)
                if r["returncode"] == 0 and r["stdout"].strip():
                    lines = r["stdout"].strip().split("\n")
                    result["total_lines_searched"] += len(lines)
                    for line in lines[-30:]:  # Last 30 matches per file
                        result["matches"].append({"source": log_path, "line": line.strip()[:500]})
        except Exception:
            pass

    # 3. Application logs (common locations)
    app_log_dirs = [
        Path.home() / ".pm2" / "logs",
        Path("/var/log/app"),
        Path("/var/log/delimit"),
    ]
    for log_dir in app_log_dirs:
        if log_dir.is_dir():
            for logfile in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
                result["sources_checked"].append({"source": str(logfile), "available": True})
                grep = shutil.which("grep")
                if grep:
                    r = _run_cmd([grep, "-i", "-n", "--text", query, str(logfile)], timeout=10)
                    if r["returncode"] == 0 and r["stdout"].strip():
                        lines = r["stdout"].strip().split("\n")
                        result["total_lines_searched"] += len(lines)
                        for line in lines[-20:]:
                            result["matches"].append({"source": str(logfile), "line": line.strip()[:500]})

    # Cap total matches
    result["matches"] = result["matches"][:100]
    result["total_matches"] = len(result["matches"])

    return result


# ─── 9. release_plan ────────────────────────────────────────────────────

def release_plan(environment: str = "production", version: str = "", repository: str = ".", services: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate a release plan from git history. Uses git only, no external integrations."""
    repo_path = Path(repository).resolve()
    if not (repo_path / ".git").is_dir():
        return {"error": "not_a_git_repo", "message": f"No .git directory found at {repo_path}"}

    cwd = str(repo_path)
    result = {
        "environment": environment,
        "repository": str(repo_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services or [],
    }

    # Get last tag
    r = _run_cmd(["git", "describe", "--tags", "--abbrev=0"], cwd=cwd)
    last_tag = r["stdout"].strip() if r["returncode"] == 0 else None
    result["last_tag"] = last_tag

    # Commits since last tag
    if last_tag:
        r = _run_cmd(["git", "log", f"{last_tag}..HEAD", "--format=%s"], cwd=cwd)
    else:
        r = _run_cmd(["git", "log", "--format=%s", "-50"], cwd=cwd)
    commits = [line.strip() for line in r["stdout"].strip().split("\n") if line.strip()] if r["stdout"].strip() else []
    result["commits_since_last_tag"] = len(commits)
    result["commits"] = commits[:30]  # Cap

    # Changed files since last tag
    if last_tag:
        r = _run_cmd(["git", "diff", "--name-only", last_tag, "HEAD"], cwd=cwd)
    else:
        r = _run_cmd(["git", "diff", "--name-only", "HEAD~10", "HEAD"], cwd=cwd)
    changed = [f for f in r["stdout"].strip().split("\n") if f.strip()] if r["stdout"].strip() else []
    result["changed_files"] = changed
    result["changed_files_count"] = len(changed)

    # Authors
    if last_tag:
        r = _run_cmd(["git", "log", f"{last_tag}..HEAD", "--format=%an"], cwd=cwd)
    else:
        r = _run_cmd(["git", "log", "--format=%an", "-50"], cwd=cwd)
    authors = list(set(line.strip() for line in r["stdout"].strip().split("\n") if line.strip())) if r["stdout"].strip() else []
    result["authors"] = authors

    # Suggest version
    if version:
        result["suggested_version"] = version
    elif last_tag:
        # Simple semver bump heuristic
        tag = last_tag.lstrip("v")
        parts = tag.split(".")
        if len(parts) == 3:
            # Check for breaking changes (MAJOR words in commits)
            commit_text = " ".join(commits).lower()
            if any(kw in commit_text for kw in ["breaking", "!:", "major"]):
                parts[0] = str(int(parts[0]) + 1)
                parts[1] = "0"
                parts[2] = "0"
            elif any(kw in commit_text for kw in ["feat", "feature", "add"]):
                parts[1] = str(int(parts[1]) + 1)
                parts[2] = "0"
            else:
                parts[2] = str(int(parts[2]) + 1)
            result["suggested_version"] = ".".join(parts)
        else:
            result["suggested_version"] = "unknown"
    else:
        result["suggested_version"] = "0.1.0"

    # Release checklist
    checklist = []

    # Tests passing?
    has_tests = any(
        (repo_path / f).exists()
        for f in ["pytest.ini", "pyproject.toml", "jest.config.js", "jest.config.ts", "vitest.config.ts", "package.json"]
    )
    checklist.append({"item": "Tests passing", "status": "check_required" if has_tests else "no_test_config", "required": True})

    # Changelog updated?
    changelog = repo_path / "CHANGELOG.md"
    if changelog.exists():
        content = changelog.read_text(errors="ignore")[:500]
        has_version = version and version in content
        checklist.append({"item": "CHANGELOG.md updated", "status": "done" if has_version else "pending", "required": True})
    else:
        checklist.append({"item": "CHANGELOG.md exists", "status": "missing", "required": False})

    # Version bumped in config?
    version_files = ["package.json", "pyproject.toml", "setup.py", "version.py", "Cargo.toml"]
    for vf in version_files:
        if (repo_path / vf).exists():
            checklist.append({"item": f"Version in {vf} updated", "status": "check_required", "required": True})
            break

    # Clean working tree?
    r = _run_cmd(["git", "status", "--porcelain"], cwd=cwd)
    clean = not r["stdout"].strip()
    checklist.append({"item": "Clean working tree", "status": "clean" if clean else "dirty", "required": True})

    # No uncommitted changes
    checklist.append({"item": "All changes committed", "status": "yes" if clean else "no", "required": True})

    # CI/CD config present?
    ci_files = [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml"]
    has_ci = any((repo_path / f).exists() for f in ci_files)
    checklist.append({"item": "CI/CD pipeline configured", "status": "present" if has_ci else "not_found", "required": False})

    result["checklist"] = checklist

    # Write plan to deploys dir
    DEPLOYS_DIR.mkdir(parents=True, exist_ok=True)
    plan_file = DEPLOYS_DIR / f"plan_{environment}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    plan_data = {
        "environment": environment,
        "version": result.get("suggested_version", version),
        "repository": str(repo_path),
        "status": "planned",
        "timestamp": result["timestamp"],
        "commits": len(commits),
        "changed_files": len(changed),
    }
    try:
        plan_file.write_text(json.dumps(plan_data, indent=2))
        result["plan_file"] = str(plan_file)
    except OSError as e:
        result["plan_file_error"] = str(e)

    return result


# ─── 10. release_status ─────────────────────────────────────────────────

def release_status(environment: str = "production") -> Dict[str, Any]:
    """Check release/deploy status from file-based tracker + git state."""
    result = {
        "environment": environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latest_deploy": None,
        "current_tag": None,
        "ahead_by_commits": 0,
        "status": "unknown",
        "deploy_history": [],
    }

    # Read from deploy tracker
    if DEPLOYS_DIR.is_dir():
        plans = sorted(DEPLOYS_DIR.glob(f"plan_{environment}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for plan_file in plans[:10]:
            try:
                data = json.loads(plan_file.read_text())
                result["deploy_history"].append(data)
            except (json.JSONDecodeError, OSError):
                pass
        if result["deploy_history"]:
            result["latest_deploy"] = result["deploy_history"][0]
            result["status"] = result["deploy_history"][0].get("status", "unknown")

    # Git state: current tag and how far HEAD is ahead
    # Try to find a repo from latest deploy, or use cwd
    repo_path = None
    if result["latest_deploy"] and result["latest_deploy"].get("repository"):
        rp = Path(result["latest_deploy"]["repository"])
        if (rp / ".git").is_dir():
            repo_path = str(rp)

    if not repo_path:
        # Fallback: check cwd
        if Path(".git").is_dir():
            repo_path = "."

    if repo_path:
        cwd = repo_path
        # Current tag
        r = _run_cmd(["git", "describe", "--tags", "--abbrev=0"], cwd=cwd)
        if r["returncode"] == 0:
            tag = r["stdout"].strip()
            result["current_tag"] = tag

            # Commits ahead of tag
            r2 = _run_cmd(["git", "rev-list", f"{tag}..HEAD", "--count"], cwd=cwd)
            if r2["returncode"] == 0:
                try:
                    result["ahead_by_commits"] = int(r2["stdout"].strip())
                except ValueError:
                    pass

            # Determine status
            if result["ahead_by_commits"] == 0:
                result["status"] = "up_to_date"
            else:
                result["status"] = "ahead_of_tag"
        else:
            result["current_tag"] = None
            result["status"] = "no_tags"

        # Current branch and HEAD
        r = _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        if r["returncode"] == 0:
            result["current_branch"] = r["stdout"].strip()
        r = _run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
        if r["returncode"] == 0:
            result["head_sha"] = r["stdout"].strip()

    return result


def _deploy_site_legacy(
    project_path: str = ".", message: str = "", env_vars: dict = None
) -> Dict[str, Any]:
    """Deploy a site project — git commit, push, Vercel build, deploy.

    Handles the full chain: commit changes, push to remote, build with env vars,
    deploy prebuilt to production. Returns deploy URL and status.
    """
    import subprocess
    from pathlib import Path

    p = Path(project_path).resolve()
    results = {"project": str(p), "steps": []}

    # 1. Check for changes
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(p),
        )
        changed_files = [l.strip() for l in status.stdout.strip().splitlines() if l.strip()]
        if not changed_files:
            return {"status": "no_changes", "message": "No changes to deploy."}
        results["changed_files"] = len(changed_files)
        results["steps"].append({"step": "check", "status": "ok", "files": len(changed_files)})
    except Exception as e:
        return {"error": f"Git status failed: {e}"}

    # 2. Preflight the git remote before creating a commit.
    try:
        result = subprocess.run(
            ["git", "push", "--dry-run", "origin", "HEAD"],
            cwd=str(p),
            timeout=30,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            results["steps"].append(
                {
                    "step": "push_precheck",
                    "status": "error",
                    "detail": (result.stderr.strip() or result.stdout.strip())[:200],
                }
            )
            results["status"] = "push_precheck_failed"
            return results
        results["steps"].append({"step": "push_precheck", "status": "ok"})
    except Exception as e:
        results["steps"].append({"step": "push_precheck", "status": "error", "detail": str(e)})
        results["status"] = "push_precheck_error"
        return results

    # 3. Vercel build
    env = {**os.environ}
    if env_vars:
        # Whitelist safe env var prefixes — block LD_PRELOAD, PATH overrides, etc.
        blocked = {
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_",
            "PATH",
            "HOME",
            "USER",
            "SHELL",
        }
        for k, v in env_vars.items():
            if not any(k.startswith(b) for b in blocked):
                env[str(k)] = str(v)

    try:
        result = subprocess.run(
            ["npx", "vercel", "build", "--prod"],
            cwd=str(p),
            timeout=120,
            capture_output=True,
            text=True,
            env=env,
        )
        results["steps"].append(
            {
                "step": "build",
                "status": "ok" if result.returncode == 0 else "error",
                "detail": (
                    result.stdout.strip()[-200:]
                    if result.returncode == 0
                    else result.stderr.strip()[:200]
                ),
            }
        )
        if result.returncode != 0:
            results["status"] = "build_failed"
            return results
    except subprocess.TimeoutExpired:
        results["steps"].append({"step": "build", "status": "timeout"})
        results["status"] = "build_timeout"
        return results
    except Exception as e:
        results["steps"].append({"step": "build", "status": "error", "detail": str(e)})
        results["status"] = "build_error"
        return results

    # 4. Git add + commit
    commit_msg = message or "deploy: site update"
    try:
        # Backward-compatible backend calls predate the repository-scoped MCP
        # contract.  Keep their call order stable, but never use an unscoped
        # ``git add -A``: stage only the paths reported by this status read.
        changed_paths = []
        for line in status.stdout.strip().splitlines():
            candidate = line[3:] if len(line) > 3 else ""
            if " -> " in candidate:
                candidate = candidate.split(" -> ", 1)[1]
            if candidate:
                changed_paths.append(candidate)
        add_cmd = ["git", "add", "--", *changed_paths]
        result = subprocess.run(add_cmd, cwd=str(p), timeout=10, capture_output=True, text=True)
        if result.returncode != 0:
            results["steps"].append(
                {
                    "step": "git_add",
                    "status": "error",
                    "detail": (result.stderr.strip() or result.stdout.strip())[:200],
                }
            )
            results["status"] = "git_add_failed"
            return results
        results["steps"].append({"step": "git_add", "status": "ok"})
    except subprocess.TimeoutExpired:
        results["steps"].append({"step": "git_add", "status": "timeout"})
        results["status"] = "git_add_timeout"
        return results
    except Exception as e:
        results["steps"].append({"step": "git_add", "status": "error", "detail": str(e)})
        results["status"] = "git_add_error"
        return results

    try:
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(p),
            timeout=10,
            capture_output=True,
            text=True,
        )
        commit_output = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode == 0:
            results["steps"].append({"step": "commit", "status": "ok", "message": commit_msg})
        elif "nothing to commit" in commit_output or "working tree clean" in commit_output:
            results["steps"].append(
                {"step": "commit", "status": "skipped", "detail": "nothing to commit"}
            )
        else:
            results["steps"].append(
                {
                    "step": "commit",
                    "status": "error",
                    "detail": (result.stderr.strip() or result.stdout.strip())[:200],
                }
            )
            results["status"] = "commit_failed"
            return results
    except Exception as e:
        results["steps"].append({"step": "commit", "status": "error", "detail": str(e)})
        results["status"] = "commit_error"
        return results

    # 5. Git push
    try:
        result = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=str(p),
            timeout=30,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            results["steps"].append(
                {
                    "step": "push",
                    "status": "error",
                    "detail": (result.stderr.strip() or result.stdout.strip())[:200],
                }
            )
            results["status"] = "push_failed"
            return results
        results["steps"].append({"step": "push", "status": "ok", "detail": "pushed"})
    except Exception as e:
        results["steps"].append({"step": "push", "status": "error", "detail": str(e)})
        results["status"] = "push_error"
        return results

    # 6. Vercel deploy
    try:
        result = subprocess.run(
            ["npx", "vercel", "deploy", "--prebuilt", "--prod"],
            cwd=str(p),
            timeout=60,
            capture_output=True,
            text=True,
            env=env,
        )
        output = result.stdout.strip()
        # Extract deploy URL
        deploy_url = ""
        for line in output.splitlines():
            if "vercel.app" in line or "delimit.ai" in line:
                deploy_url = line.strip()
                break
        results["steps"].append(
            {
                "step": "deploy",
                "status": "ok" if result.returncode == 0 else "error",
                "url": deploy_url,
            }
        )
        if result.returncode != 0:
            results["status"] = "deploy_failed"
            return results
        results["deploy_url"] = deploy_url
    except subprocess.TimeoutExpired as exc:
        pending_output = "\n".join(
            str(value or "") for value in (getattr(exc, "stdout", ""), getattr(exc, "stderr", ""))
        )
        results["steps"].append(
            {
                "step": "deploy",
                "status": "pending",
                "detail": "Vercel command timed out after push",
            }
        )
        results["status"] = "pending"
        results.update(_extract_vercel_deployment_metadata(pending_output))
        return results
    except Exception as e:
        results["steps"].append({"step": "deploy", "status": "error", "detail": str(e)})
        results["status"] = "deploy_error"
        return results

    results["status"] = "deployed"
    return results


def _validate_scoped_paths(repo: Path, paths: Optional[List[str]]) -> List[str]:
    """Return normalized repo-relative pathspecs without option/path escapes."""
    normalized: List[str] = []
    for raw_path in paths or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("paths entries must be non-empty strings")
        if raw_path.startswith("-") or "\x00" in raw_path:
            raise ValueError(f"unsafe deploy path: {raw_path!r}")
        candidate = (repo / raw_path).resolve()
        try:
            relative = candidate.relative_to(repo)
        except ValueError as exc:
            raise ValueError(f"deploy path escapes repository: {raw_path}") from exc
        normalized_path = relative.as_posix()
        if normalized_path == ".":
            raise ValueError("repository root is not an explicit deploy path")
        normalized.append(normalized_path)
    return list(dict.fromkeys(normalized))


def _parse_nul_paths(output: str) -> List[str]:
    """Parse NUL-delimited Git output (newline fallback supports old Git/mocks)."""
    if not output:
        return []
    values = output.split("\0") if "\0" in output else output.splitlines()
    return [value.strip() for value in values if value.strip()]


def _read_vercel_project(project: Path) -> Optional[Dict[str, Any]]:
    """Validate Vercel's local binding without returning its identifiers."""
    project_file = project / ".vercel" / "project.json"
    try:
        data = json.loads(project_file.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("projectId"), str) or not data["projectId"].strip():
        return None
    if not isinstance(data.get("orgId"), str) or not data["orgId"].strip():
        return None
    return {"configured": True, "source": str(project_file)}


def _extract_vercel_deployment_metadata(output: str) -> Dict[str, str]:
    """Extract non-secret continuation identifiers from bounded CLI output."""
    metadata: Dict[str, str] = {}
    url_match = re.search(r"https://[^\s]+", output or "")
    if url_match:
        metadata["deploy_url"] = url_match.group(0).rstrip(".,)")
    id_match = re.search(r"\b(dpl_[A-Za-z0-9]+)\b", output or "")
    if id_match:
        metadata["deployment_id"] = id_match.group(1)
    return metadata


def _scoped_git_result(
    cmd: List[str],
    *,
    cwd: Path,
    timeout: int,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run one bounded command for the repository-scoped deploy pipeline."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        timeout=timeout,
        capture_output=True,
        text=True,
        env=env,
    )


def _deploy_site_scoped(
    *,
    repo_path: str,
    project_path: str,
    app: str,
    message: str,
    env_vars: Optional[dict],
    paths: Optional[List[str]],
    staged_only: bool,
    vercel_timeout: int,
) -> Dict[str, Any]:
    """Repository-scoped site deployment used by the public MCP surface."""
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return {
            "status": "invalid_repository",
            "error": f"Repository does not exist: {repo}",
        }

    project = Path(project_path).expanduser()
    project = (repo / project).resolve() if not project.is_absolute() else project.resolve()
    try:
        project.relative_to(repo)
    except ValueError:
        return {
            "status": "invalid_project",
            "error": "project_path must be inside repo_path",
        }
    if not project.is_dir():
        return {
            "status": "invalid_project",
            "error": f"Project does not exist: {project}",
        }

    context = _scoped_git_result(["git", "rev-parse", "--show-toplevel"], cwd=repo, timeout=10)
    if context.returncode != 0:
        return {
            "status": "invalid_repository",
            "error": (context.stderr.strip() or "repo_path is not a Git worktree")[:300],
            "repo_path": str(repo),
        }
    try:
        git_root = Path(context.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        git_root = Path("")
    if git_root != repo:
        return {
            "status": "invalid_repository",
            "error": "repo_path must be the Git worktree root",
            "repo_path": str(repo),
            "git_root": str(git_root),
        }

    try:
        scoped_paths = _validate_scoped_paths(repo, paths)
    except ValueError as exc:
        return {"status": "invalid_scope", "error": str(exc), "repo_path": str(repo)}
    if staged_only and scoped_paths:
        return {
            "status": "invalid_scope",
            "error": "Choose staged_only=true or explicit paths, not both",
            "repo_path": str(repo),
        }
    if not staged_only and not scoped_paths:
        return {
            "status": "scope_required",
            "error": "Explicit paths are required when staged_only is false",
            "repo_path": str(repo),
        }

    staged = _scoped_git_result(
        ["git", "diff", "--cached", "--name-only", "-z"], cwd=repo, timeout=10
    )
    if staged.returncode != 0:
        return {
            "status": "git_status_failed",
            "error": staged.stderr.strip()[:300],
            "repo_path": str(repo),
        }
    staged_paths = _parse_nul_paths(staged.stdout)
    if staged_only:
        selected_paths = staged_paths
        if not selected_paths:
            return {
                "status": "no_staged_changes",
                "error": "No staged changes. Stage the intended files or pass explicit paths.",
                "repo_path": str(repo),
            }
    else:

        def authorized_by_scope(staged_path: str) -> bool:
            for scope in scoped_paths:
                if staged_path == scope:
                    return True
                if staged_path.startswith(scope.rstrip("/") + "/"):
                    return True
            return False

        extras = sorted(path for path in staged_paths if not authorized_by_scope(path))
        if extras:
            return {
                "status": "unsafe_staged_changes",
                "error": "The index contains changes outside the requested deploy paths",
                "unexpected_staged_paths": extras,
                "repo_path": str(repo),
            }
        selected_paths = scoped_paths

    env = dict(os.environ)
    if env_vars:
        blocked = {
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_",
            "PATH",
            "HOME",
            "USER",
            "SHELL",
        }
        for key, value in env_vars.items():
            if not any(str(key).startswith(prefix) for prefix in blocked):
                env[str(key)] = str(value)

    results: Dict[str, Any] = {
        "app": app,
        "project": app or project.name,
        "repo_path": str(repo),
        "project_path": str(project),
        "scope": "staged_only" if staged_only else "explicit_paths",
        "selected_paths": selected_paths,
        "steps": [],
    }

    vercel_config = _read_vercel_project(project)
    if vercel_config is None:
        pull = _scoped_git_result(
            ["npx", "vercel", "pull", "--yes", "--environment=production"],
            cwd=project,
            timeout=60,
            env=env,
        )
        if pull.returncode != 0 or _read_vercel_project(project) is None:
            results["status"] = "vercel_setup_required"
            results["steps"].append(
                {
                    "step": "vercel_setup",
                    "status": "error",
                    "detail": (pull.stderr.strip() or "Vercel project settings were not created")[
                        :300
                    ],
                }
            )
            results["hint"] = "Link this project to Vercel, then retry; no commit or push occurred."
            return results
        vercel_config = _read_vercel_project(project)
        results["steps"].append({"step": "vercel_setup", "status": "pulled"})
    else:
        results["steps"].append({"step": "vercel_setup", "status": "validated"})
    results["vercel_project"] = vercel_config

    # Explicit paths start unstaged by definition. Stage exactly those paths
    # only after the Vercel binding has been validated, then inspect the whole
    # project for any other dirty input before building.
    if not staged_only:
        add_result = _scoped_git_result(["git", "add", "--", *selected_paths], cwd=repo, timeout=10)
        if add_result.returncode != 0:
            results["status"] = "git_add_failed"
            results["steps"].append(
                {
                    "step": "git_add",
                    "status": "error",
                    "detail": add_result.stderr.strip()[:300],
                }
            )
            return results
        results["steps"].append({"step": "git_add", "status": "ok"})
        staged_after_add = _scoped_git_result(
            ["git", "diff", "--cached", "--name-only", "-z"], cwd=repo, timeout=10
        )
        if staged_after_add.returncode != 0:
            results["status"] = "git_status_failed"
            results["steps"].append(
                {
                    "step": "git_status_after_add",
                    "status": "error",
                    "detail": staged_after_add.stderr.strip()[:300],
                }
            )
            return results
        newly_staged_paths = sorted(
            set(_parse_nul_paths(staged_after_add.stdout)) - set(staged_paths)
        )

    project_rel = project.relative_to(repo).as_posix()
    project_scope = [] if project_rel == "." else ["--", project_rel]
    dirty = _scoped_git_result(
        ["git", "status", "--porcelain=v1", "-z", *project_scope], cwd=repo, timeout=10
    )
    if dirty.returncode != 0:
        return {
            "status": "git_status_failed",
            "error": dirty.stderr.strip()[:300],
            "repo_path": str(repo),
        }
    # The build reads the working tree. Refuse unstaged/untracked inputs in the
    # deployed project so the Vercel artifact cannot differ from the commit.
    unsafe_dirty: List[str] = []
    entries = dirty.stdout.split("\0") if "\0" in dirty.stdout else dirty.stdout.splitlines()
    for entry in entries:
        if not entry:
            continue
        state = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if state == "??" or len(state) < 2 or state[1] != " ":
            unsafe_dirty.append(path)
    if unsafe_dirty:
        cleanup_error = ""
        if not staged_only and newly_staged_paths:
            cleanup = _scoped_git_result(
                ["git", "reset", "--quiet", "HEAD", "--", *newly_staged_paths],
                cwd=repo,
                timeout=10,
            )
            if cleanup.returncode != 0:
                cleanup_error = cleanup.stderr.strip()[:300]
        return {
            "status": "unsafe_dirty_project",
            "error": "Unstaged or untracked project files could contaminate the build",
            "dirty_paths": unsafe_dirty,
            "repo_path": str(repo),
            "project_path": str(project),
            "index_restored": not cleanup_error,
            **({"index_cleanup_error": cleanup_error} if cleanup_error else {}),
        }

    precheck = _scoped_git_result(
        ["git", "push", "--dry-run", "origin", "HEAD"], cwd=repo, timeout=30
    )
    if precheck.returncode != 0:
        results["status"] = "push_precheck_failed"
        results["steps"].append(
            {
                "step": "push_precheck",
                "status": "error",
                "detail": (precheck.stderr or precheck.stdout).strip()[:300],
            }
        )
        return results
    results["steps"].append({"step": "push_precheck", "status": "ok"})

    try:
        build_result = _scoped_git_result(
            ["npx", "vercel", "build", "--prod"], cwd=project, timeout=120, env=env
        )
    except subprocess.TimeoutExpired:
        results["status"] = "build_timeout"
        results["steps"].append({"step": "build", "status": "timeout"})
        return results
    if build_result.returncode != 0:
        results["status"] = "build_failed"
        results["steps"].append(
            {
                "step": "build",
                "status": "error",
                "detail": build_result.stderr.strip()[:300],
            }
        )
        return results
    results["steps"].append({"step": "build", "status": "ok"})

    commit_result = _scoped_git_result(
        ["git", "commit", "-m", message or "deploy: site update"], cwd=repo, timeout=120
    )
    if commit_result.returncode != 0:
        results["status"] = "commit_failed"
        results["steps"].append(
            {
                "step": "commit",
                "status": "error",
                "detail": (commit_result.stderr or commit_result.stdout).strip()[:300],
            }
        )
        return results
    sha_result = _scoped_git_result(["git", "rev-parse", "HEAD"], cwd=repo, timeout=10)
    commit_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""
    results["commit_sha"] = commit_sha
    results["steps"].append({"step": "commit", "status": "ok", "commit_sha": commit_sha})

    push_result = _scoped_git_result(["git", "push", "origin", "HEAD"], cwd=repo, timeout=60)
    if push_result.returncode != 0:
        results["status"] = "push_failed"
        results["steps"].append(
            {
                "step": "push",
                "status": "error",
                "detail": push_result.stderr.strip()[:300],
            }
        )
        return results
    results["steps"].append({"step": "push", "status": "ok"})

    try:
        deploy_result = _scoped_git_result(
            ["npx", "vercel", "deploy", "--prebuilt", "--prod"],
            cwd=project,
            timeout=max(10, min(int(vercel_timeout), 600)),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_output = "\n".join(
            str(value or "") for value in (getattr(exc, "stdout", ""), getattr(exc, "stderr", ""))
        )
        results.update(_extract_vercel_deployment_metadata(timeout_output))
        results["status"] = "pending"
        results["steps"].append(
            {
                "step": "deploy",
                "status": "pending",
                "detail": "Vercel timed out after push; continue with deploy_verify",
            }
        )
        results["continuation"] = {
            "tool": "delimit_deploy_verify",
            "app": app,
            "repo_path": str(repo),
            "git_ref": commit_sha,
        }
        return results
    output = "\n".join((deploy_result.stdout or "", deploy_result.stderr or ""))
    results.update(_extract_vercel_deployment_metadata(output))
    if deploy_result.returncode != 0:
        results["status"] = "deploy_failed"
        results["steps"].append(
            {
                "step": "deploy",
                "status": "error",
                "detail": deploy_result.stderr.strip()[:300],
            }
        )
        return results
    results["steps"].append(
        {"step": "deploy", "status": "ok", "url": results.get("deploy_url", "")}
    )
    results["status"] = "deployed"
    return results


def deploy_site(
    project_path: str = ".",
    message: str = "",
    env_vars: dict = None,
    *,
    repo_path: str = "",
    app: str = "",
    paths: Optional[List[str]] = None,
    staged_only: Optional[bool] = None,
    vercel_timeout: int = 60,
) -> Dict[str, Any]:
    """Deploy a Vercel site with an explicit repository and change scope.

    MCP callers pass ``repo_path`` and either retain the safe staged-only
    default or provide explicit ``paths``. Direct legacy backend callers keep
    their historical call order, while their staging command is path-scoped.
    """
    if staged_only is None and not repo_path and paths is None and not app:
        return _deploy_site_legacy(project_path, message, env_vars)
    effective_repo = repo_path or project_path
    effective_project = project_path if repo_path else "."
    return _deploy_site_scoped(
        repo_path=effective_repo,
        project_path=effective_project,
        app=app,
        message=message,
        env_vars=env_vars,
        paths=paths,
        staged_only=True if staged_only is None else staged_only,
        vercel_timeout=vercel_timeout,
    )

def deploy_npm(project_path: str = ".", bump: str = "patch", tag: str = "latest", dry_run: bool = False) -> Dict[str, Any]:
    """Publish an npm package — bump version, publish, verify.

    Handles: version bump (patch/minor/major), npm publish, verify on registry.
    Optionally dry-run to preview without publishing.
    """
    import subprocess
    from pathlib import Path

    p = Path(project_path).resolve()
    pkg_json = p / "package.json"

    if not pkg_json.exists():
        return {"error": f"No package.json found at {p}"}

    results = {"project": str(p), "steps": []}

    # 1. Read current version
    try:
        import json
        with open(pkg_json) as f:
            pkg = json.load(f)
        current_version = pkg.get("version", "0.0.0")
        pkg_name = pkg.get("name", "unknown")
        results["package"] = pkg_name
        results["current_version"] = current_version
        results["steps"].append({"step": "read_version", "status": "ok", "version": current_version})
    except Exception as e:
        return {"error": f"Failed to read package.json: {e}"}

    # 2. Check npm auth
    try:
        result = subprocess.run(
            ["npm", "whoami"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return {"error": "Not logged into npm. Run: npm login"}
        npm_user = result.stdout.strip()
        results["npm_user"] = npm_user
        results["steps"].append({"step": "auth_check", "status": "ok", "user": npm_user})
    except Exception as e:
        return {"error": f"npm auth check failed: {e}"}

    # 3. Check for uncommitted changes
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, cwd=str(p)
        )
        uncommitted = [l.strip() for l in status.stdout.strip().splitlines() if l.strip()]
        if uncommitted:
            results["steps"].append({"step": "git_check", "status": "warning", "uncommitted_files": len(uncommitted)})
        else:
            results["steps"].append({"step": "git_check", "status": "ok"})
    except Exception:
        pass

    # ── Dry-run: simulate without touching the filesystem ──
    if dry_run:
        # Compute what the next version would be without actually bumping
        parts = current_version.split(".")
        if len(parts) == 3 and bump in ("patch", "minor", "major"):
            major, minor, patch_v = int(parts[0]), int(parts[1]), int(parts[2])
            if bump == "patch":
                patch_v += 1
            elif bump == "minor":
                minor += 1
                patch_v = 0
            elif bump == "major":
                major += 1
                minor = 0
                patch_v = 0
            simulated_version = f"{major}.{minor}.{patch_v}"
        else:
            simulated_version = current_version
        results["new_version"] = simulated_version
        results["steps"].append({"step": "version_bump", "status": "dry_run", "from": current_version, "to": simulated_version, "bump": bump})
        results["steps"].append({"step": "publish", "status": "dry_run", "tag": tag, "output": f"Would publish {pkg_name}@{simulated_version} with tag {tag}"})
        results["steps"].append({"step": "verify", "status": "dry_run"})
        results["status"] = "dry_run_complete"
        return results

    # 4. Version bump (dry_run already returned above, so this is always a real bump)
    if bump in ("patch", "minor", "major"):
        try:
            bump_cmd = ["npm", "version", bump, "--no-git-tag-version"]
            result = subprocess.run(
                bump_cmd, capture_output=True, text=True, timeout=10, cwd=str(p)
            )
            if result.returncode == 0:
                new_version = result.stdout.strip().lstrip("v")
                results["new_version"] = new_version
                results["steps"].append({"step": "version_bump", "status": "ok", "from": current_version, "to": new_version, "bump": bump})
            else:
                results["steps"].append({"step": "version_bump", "status": "error", "detail": result.stderr.strip()[:200]})
                results["status"] = "bump_failed"
                return results
        except Exception as e:
            results["steps"].append({"step": "version_bump", "status": "error", "detail": str(e)})
            results["status"] = "bump_failed"
            return results
    else:
        results["new_version"] = current_version

    # 5. Publish
    publish_cmd = ["npm", "publish", "--tag", tag]

    try:
        result = subprocess.run(
            publish_cmd, capture_output=True, text=True, timeout=60, cwd=str(p)
        )
        if result.returncode == 0:
            results["steps"].append({
                "step": "publish",
                "status": "ok",
                "tag": tag,
                "output": result.stdout.strip()[-300:]
            })
        else:
            results["steps"].append({
                "step": "publish",
                "status": "error",
                "detail": result.stderr.strip()[:300]
            })
            results["status"] = "publish_failed"
            return results
    except subprocess.TimeoutExpired:
        results["steps"].append({"step": "publish", "status": "timeout"})
        results["status"] = "publish_timeout"
        return results
    except Exception as e:
        results["steps"].append({"step": "publish", "status": "error", "detail": str(e)})
        results["status"] = "publish_failed"
        return results

    # 6. Verify on registry
    try:
        import time
        time.sleep(2)  # brief wait for registry propagation
        result = subprocess.run(
            ["npm", "view", pkg_name, "version"],
            capture_output=True, text=True, timeout=15
        )
        registry_version = result.stdout.strip()
        verified = registry_version == results.get("new_version", current_version)
        results["steps"].append({
            "step": "verify",
            "status": "ok" if verified else "mismatch",
            "registry_version": registry_version
        })
    except Exception:
        results["steps"].append({"step": "verify", "status": "skipped"})

    # 7. Git commit the version bump
    if bump in ("patch", "minor", "major"):
        try:
            new_ver = results.get("new_version", current_version)
            subprocess.run(["git", "add", "package.json"], cwd=str(p), timeout=10, capture_output=True)
            # Also stage package-lock.json if it exists
            lock_file = p / "package-lock.json"
            if lock_file.exists():
                subprocess.run(["git", "add", "package-lock.json"], cwd=str(p), timeout=10, capture_output=True)
            result = subprocess.run(
                ["git", "commit", "-m", f"release: v{new_ver}"],
                cwd=str(p), timeout=10, capture_output=True, text=True
            )
            if result.returncode == 0:
                results["steps"].append({"step": "git_commit", "status": "ok", "message": f"release: v{new_ver}"})
                # Push
                push_result = subprocess.run(
                    ["git", "push", "origin", "HEAD"],
                    cwd=str(p), timeout=30, capture_output=True, text=True
                )
                results["steps"].append({
                    "step": "git_push",
                    "status": "ok" if push_result.returncode == 0 else "error"
                })
        except Exception as e:
            results["steps"].append({"step": "git_commit", "status": "error", "detail": str(e)})

    results["status"] = "published" if not dry_run else "dry_run_complete"
    return results
