"""
Real implementations for cost, data, and intel tools.
All tools work WITHOUT external integrations by default, using file-based
analysis and local storage. Optional cloud API integration when keys are configured.
"""

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.tools_data")

DELIMIT_HOME = Path(os.environ.get("DELIMIT_HOME", os.path.expanduser("~/.delimit")))
BACKUPS_DIR = DELIMIT_HOME / "backups"
INTEL_DIR = DELIMIT_HOME / "intel"
COST_ALERTS_FILE = DELIMIT_HOME / "cost_alerts.json"
DATASETS_FILE = INTEL_DIR / "datasets.json"
SNAPSHOTS_DIR = INTEL_DIR / "snapshots"

# Typical VPS monthly pricing estimates (USD)
VPS_COST_ESTIMATES = {
    "small": 5.0,    # 1 vCPU, 1GB RAM
    "medium": 20.0,  # 2 vCPU, 4GB RAM
    "large": 40.0,   # 4 vCPU, 8GB RAM
    "xlarge": 80.0,  # 8 vCPU, 16GB RAM
}


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════
#  COST TOOLS
# ═══════════════════════════════════════════════════════════════════════


def cost_analyze(target: str = ".") -> Dict[str, Any]:
    """Analyze project costs by scanning infrastructure files."""
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    services = []
    cost_breakdown = []
    recommendations = []
    total_cost = 0.0

    # Scan for Dockerfiles
    dockerfiles = list(target_path.rglob("Dockerfile")) + list(target_path.rglob("Dockerfile.*"))
    for df in dockerfiles:
        rel = str(df.relative_to(target_path))
        content = df.read_text(errors="ignore")
        # Estimate size category from base image
        size = "medium"
        if any(kw in content.lower() for kw in ["alpine", "slim", "distroless"]):
            size = "small"
        elif any(kw in content.lower() for kw in ["gpu", "cuda", "nvidia"]):
            size = "xlarge"
        est = VPS_COST_ESTIMATES[size]
        services.append({"type": "container", "file": rel, "size_estimate": size})
        cost_breakdown.append({"item": f"Container ({rel})", "monthly_usd": est})
        total_cost += est

    # Scan for docker-compose
    compose_files = list(target_path.rglob("docker-compose.yml")) + list(target_path.rglob("docker-compose.yaml")) + list(target_path.rglob("compose.yml")) + list(target_path.rglob("compose.yaml"))
    for cf in compose_files:
        try:
            content = cf.read_text(errors="ignore")
            # Count services by looking for service blocks
            svc_count = len(re.findall(r"^\s{2}\w[\w-]*:\s*$", content, re.MULTILINE))
            if svc_count == 0:
                # Fallback: count lines that look like service definitions
                svc_count = max(1, content.lower().count("image:"))
            rel = str(cf.relative_to(target_path))
            for i in range(svc_count):
                est = VPS_COST_ESTIMATES["medium"]
                services.append({"type": "compose_service", "file": rel, "index": i})
                cost_breakdown.append({"item": f"Compose service #{i+1} ({rel})", "monthly_usd": est})
                total_cost += est
        except Exception:
            pass

    # Scan for package.json (npm dependencies)
    pkg_files = list(target_path.rglob("package.json"))
    dep_count = 0
    for pf in pkg_files:
        if "node_modules" in str(pf):
            continue
        try:
            data = json.loads(pf.read_text(errors="ignore"))
            deps = len(data.get("dependencies", {}))
            dev_deps = len(data.get("devDependencies", {}))
            dep_count += deps + dev_deps
            services.append({"type": "node_project", "file": str(pf.relative_to(target_path)), "dependencies": deps, "dev_dependencies": dev_deps})
        except Exception:
            pass

    # Scan for requirements.txt (Python dependencies)
    req_files = list(target_path.rglob("requirements.txt")) + list(target_path.rglob("requirements/*.txt"))
    for rf in req_files:
        try:
            lines = [l.strip() for l in rf.read_text(errors="ignore").splitlines() if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("-")]
            dep_count += len(lines)
            services.append({"type": "python_project", "file": str(rf.relative_to(target_path)), "dependencies": len(lines)})
        except Exception:
            pass

    # Scan for pyproject.toml
    pyproject_files = list(target_path.rglob("pyproject.toml"))
    for pf in pyproject_files:
        if "node_modules" in str(pf):
            continue
        try:
            content = pf.read_text(errors="ignore")
            dep_lines = re.findall(r'^\s*"[^"]+[><=!]', content, re.MULTILINE)
            if dep_lines:
                dep_count += len(dep_lines)
                services.append({"type": "python_project", "file": str(pf.relative_to(target_path)), "dependencies": len(dep_lines)})
        except Exception:
            pass

    # Scan for cloud config files
    cloud_providers = []
    aws_paths = [target_path / ".aws", Path.home() / ".aws"]
    for ap in aws_paths:
        if ap.exists():
            cloud_providers.append("aws")
            break

    gcloud_paths = [target_path / ".gcloud", Path.home() / ".config" / "gcloud"]
    for gp in gcloud_paths:
        if gp.exists():
            cloud_providers.append("gcp")
            break

    if (target_path / ".azure").exists() or (Path.home() / ".azure").exists():
        cloud_providers.append("azure")

    # Check for Vercel / Netlify / Railway configs
    for conf, provider in [("vercel.json", "vercel"), ("netlify.toml", "netlify"), ("railway.json", "railway"), ("fly.toml", "fly.io")]:
        if (target_path / conf).exists():
            cloud_providers.append(provider)
            services.append({"type": "paas", "provider": provider, "file": conf})
            cost_breakdown.append({"item": f"PaaS ({provider})", "monthly_usd": 10.0})
            total_cost += 10.0

    # If no services found, report that
    if not services:
        recommendations.append("No infrastructure files detected. Add Dockerfiles or deployment configs for cost estimation.")

    if dep_count > 100:
        recommendations.append(f"High dependency count ({dep_count}). Consider auditing for unused packages to reduce build times and attack surface.")
    if len(dockerfiles) > 0 and not any("alpine" in df.read_text(errors="ignore").lower() or "slim" in df.read_text(errors="ignore").lower() for df in dockerfiles):
        recommendations.append("Consider using Alpine or slim base images to reduce container costs.")

    return {
        "tool": "cost.analyze",
        "target": str(target_path),
        "estimated_monthly_cost": round(total_cost, 2),
        "services_detected": len(services),
        "services": services,
        "dependency_count": dep_count,
        "cloud_providers": cloud_providers,
        "cost_breakdown": cost_breakdown,
        "recommendations": recommendations,
    }


def cost_optimize(target: str = ".") -> Dict[str, Any]:
    """Find cost optimization opportunities in a project."""
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    opportunities = []
    estimated_savings = 0.0

    # Check for oversized Docker images (no multi-stage build)
    dockerfiles = list(target_path.rglob("Dockerfile")) + list(target_path.rglob("Dockerfile.*"))
    for df in dockerfiles:
        content = df.read_text(errors="ignore")
        rel = str(df.relative_to(target_path))
        from_count = len(re.findall(r"^FROM\s+", content, re.MULTILINE | re.IGNORECASE))

        if from_count == 1 and "AS" not in content.upper().split("FROM", 1)[-1].split("\n")[0]:
            opportunities.append({
                "type": "docker_multistage",
                "file": rel,
                "severity": "medium",
                "description": "Single-stage Docker build. Multi-stage builds can reduce image size by 50-80%.",
                "estimated_savings_usd": 5.0,
            })
            estimated_savings += 5.0

        if not any(kw in content.lower() for kw in ["alpine", "slim", "distroless", "scratch"]):
            opportunities.append({
                "type": "docker_base_image",
                "file": rel,
                "severity": "low",
                "description": "Using full base image. Alpine/slim variants reduce image size and pull costs.",
                "estimated_savings_usd": 2.0,
            })
            estimated_savings += 2.0

        # Check for .dockerignore
        dockerignore = df.parent / ".dockerignore"
        if not dockerignore.exists():
            opportunities.append({
                "type": "missing_dockerignore",
                "file": rel,
                "severity": "low",
                "description": "No .dockerignore found. Build context may include unnecessary files.",
                "estimated_savings_usd": 1.0,
            })
            estimated_savings += 1.0

    # Check for unused dependencies in package.json
    pkg_files = list(target_path.rglob("package.json"))
    for pf in pkg_files:
        if "node_modules" in str(pf):
            continue
        try:
            data = json.loads(pf.read_text(errors="ignore"))
            deps = data.get("dependencies", {})
            rel = str(pf.relative_to(target_path))

            # Scan source files for import references
            src_dir = pf.parent / "src"
            if not src_dir.exists():
                src_dir = pf.parent

            source_content = ""
            for ext in ["*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs"]:
                for sf in src_dir.rglob(ext):
                    if "node_modules" in str(sf) or "dist" in str(sf) or ".next" in str(sf):
                        continue
                    try:
                        source_content += sf.read_text(errors="ignore") + "\n"
                    except Exception:
                        pass

            if source_content:
                potentially_unused = []
                for dep_name in deps:
                    # Check common import patterns
                    patterns = [
                        dep_name,
                        dep_name.replace("-", "_"),
                        dep_name.split("/")[-1],
                    ]
                    if not any(p in source_content for p in patterns):
                        potentially_unused.append(dep_name)

                if potentially_unused:
                    opportunities.append({
                        "type": "unused_npm_dependencies",
                        "file": rel,
                        "severity": "medium",
                        "description": f"Potentially unused dependencies: {', '.join(potentially_unused[:10])}",
                        "count": len(potentially_unused),
                        "packages": potentially_unused[:10],
                        "estimated_savings_usd": len(potentially_unused) * 0.5,
                    })
                    estimated_savings += len(potentially_unused) * 0.5
        except Exception:
            pass

    # Check for uncompressed assets
    large_assets = []
    for ext in ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.svg"]:
        for af in target_path.rglob(ext):
            if "node_modules" in str(af) or ".git" in str(af):
                continue
            try:
                size = af.stat().st_size
                if size > 500_000:  # > 500KB
                    large_assets.append({"file": str(af.relative_to(target_path)), "size_bytes": size})
            except Exception:
                pass

    if large_assets:
        opportunities.append({
            "type": "uncompressed_assets",
            "severity": "low",
            "description": f"Found {len(large_assets)} large image files (>500KB). Consider compression or WebP conversion.",
            "files": large_assets[:10],
            "estimated_savings_usd": 2.0,
        })
        estimated_savings += 2.0

    # Check for uncompressed JS/CSS bundles
    for ext in ["*.js", "*.css"]:
        for bf in target_path.rglob(ext):
            if "node_modules" in str(bf) or ".git" in str(bf):
                continue
            parts = str(bf).lower()
            if any(d in parts for d in ["/dist/", "/build/", "/public/", "/.next/"]):
                try:
                    size = bf.stat().st_size
                    if size > 1_000_000:  # > 1MB
                        gz_path = Path(str(bf) + ".gz")
                        br_path = Path(str(bf) + ".br")
                        if not gz_path.exists() and not br_path.exists():
                            opportunities.append({
                                "type": "uncompressed_bundle",
                                "file": str(bf.relative_to(target_path)),
                                "severity": "medium",
                                "size_bytes": size,
                                "description": f"Large uncompressed bundle ({size // 1024}KB). Enable gzip/brotli compression.",
                                "estimated_savings_usd": 1.0,
                            })
                            estimated_savings += 1.0
                except Exception:
                    pass

    return {
        "tool": "cost.optimize",
        "target": str(target_path),
        "optimization_opportunities": opportunities,
        "opportunity_count": len(opportunities),
        "estimated_savings": round(estimated_savings, 2),
    }


def cost_alert(action: str = "list", name: Optional[str] = None,
               threshold: Optional[float] = None, alert_id: Optional[str] = None) -> Dict[str, Any]:
    """Manage file-based cost alerts."""
    _ensure_dir(DELIMIT_HOME)

    # Load existing alerts
    alerts = []
    if COST_ALERTS_FILE.exists():
        try:
            alerts = json.loads(COST_ALERTS_FILE.read_text())
        except Exception:
            alerts = []

    if action == "list":
        return {
            "tool": "cost.alert",
            "action": "list",
            "alerts": alerts,
            "active_count": sum(1 for a in alerts if a.get("active", True)),
        }

    elif action == "create":
        if not name or threshold is None:
            return {"error": "missing_params", "message": "create requires 'name' and 'threshold'"}
        new_alert = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "threshold_usd": threshold,
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        alerts.append(new_alert)
        COST_ALERTS_FILE.write_text(json.dumps(alerts, indent=2))
        return {
            "tool": "cost.alert",
            "action": "create",
            "alert": new_alert,
            "alerts": alerts,
            "active_count": sum(1 for a in alerts if a.get("active", True)),
        }

    elif action == "delete":
        if not alert_id:
            return {"error": "missing_params", "message": "delete requires 'alert_id'"}
        original_count = len(alerts)
        alerts = [a for a in alerts if a.get("id") != alert_id]
        if len(alerts) == original_count:
            return {"error": "not_found", "message": f"Alert {alert_id} not found"}
        COST_ALERTS_FILE.write_text(json.dumps(alerts, indent=2))
        return {
            "tool": "cost.alert",
            "action": "delete",
            "deleted_id": alert_id,
            "alerts": alerts,
            "active_count": sum(1 for a in alerts if a.get("active", True)),
        }

    elif action == "toggle":
        if not alert_id:
            return {"error": "missing_params", "message": "toggle requires 'alert_id'"}
        found = False
        for a in alerts:
            if a.get("id") == alert_id:
                a["active"] = not a.get("active", True)
                found = True
                break
        if not found:
            return {"error": "not_found", "message": f"Alert {alert_id} not found"}
        COST_ALERTS_FILE.write_text(json.dumps(alerts, indent=2))
        return {
            "tool": "cost.alert",
            "action": "toggle",
            "alert_id": alert_id,
            "alerts": alerts,
            "active_count": sum(1 for a in alerts if a.get("active", True)),
        }

    else:
        return {"error": "invalid_action", "message": f"Unknown action: {action}. Use list/create/delete/toggle."}


# ═══════════════════════════════════════════════════════════════════════
#  DATA TOOLS
# ═══════════════════════════════════════════════════════════════════════


def data_validate(target: str = ".") -> Dict[str, Any]:
    """Validate data files in a directory."""
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    files_checked = 0
    valid = 0
    invalid = 0
    issues = []

    # If target is a single file, validate just that
    if target_path.is_file():
        file_list = [target_path]
    else:
        file_list = []
        for ext in ["*.json", "*.csv", "*.sqlite", "*.sqlite3", "*.db"]:
            for f in target_path.rglob(ext):
                if "node_modules" in str(f) or ".git" in str(f):
                    continue
                file_list.append(f)

    for fpath in file_list:
        files_checked += 1
        suffix = fpath.suffix.lower()
        rel = str(fpath.relative_to(target_path)) if target_path.is_dir() else fpath.name

        if suffix == ".json":
            try:
                content = fpath.read_text(errors="ignore")
                json.loads(content)
                valid += 1
            except json.JSONDecodeError as e:
                invalid += 1
                issues.append({"file": rel, "type": "json_parse_error", "message": str(e)})
            except Exception as e:
                invalid += 1
                issues.append({"file": rel, "type": "read_error", "message": str(e)})

        elif suffix == ".csv":
            try:
                content = fpath.read_text(errors="ignore")
                reader = csv.reader(io.StringIO(content))
                rows = list(reader)
                if not rows:
                    issues.append({"file": rel, "type": "empty_csv", "message": "CSV file is empty"})
                    invalid += 1
                else:
                    header_len = len(rows[0])
                    inconsistent_rows = []
                    for i, row in enumerate(rows[1:], start=2):
                        if len(row) != header_len:
                            inconsistent_rows.append(i)
                    if inconsistent_rows:
                        invalid += 1
                        issues.append({
                            "file": rel,
                            "type": "csv_column_mismatch",
                            "message": f"Expected {header_len} columns, found mismatches on rows: {inconsistent_rows[:10]}",
                            "expected_columns": header_len,
                            "mismatched_rows": inconsistent_rows[:10],
                        })
                    else:
                        valid += 1
            except Exception as e:
                invalid += 1
                issues.append({"file": rel, "type": "csv_error", "message": str(e)})

        elif suffix in (".sqlite", ".sqlite3", ".db"):
            try:
                conn = sqlite3.connect(str(fpath))
                cursor = conn.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                if result and result[0] == "ok":
                    # Also get table count
                    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    valid += 1
                    if not tables:
                        issues.append({"file": rel, "type": "empty_database", "message": "SQLite database has no tables", "severity": "info"})
                else:
                    invalid += 1
                    issues.append({"file": rel, "type": "sqlite_integrity_failed", "message": str(result)})
                conn.close()
            except Exception as e:
                invalid += 1
                issues.append({"file": rel, "type": "sqlite_error", "message": str(e)})

    return {
        "tool": "data.validate",
        "target": str(target_path),
        "files_checked": files_checked,
        "valid": valid,
        "invalid": invalid,
        "issues": issues,
    }


def data_migrate(target: str = ".") -> Dict[str, Any]:
    """Check for migration files and report status."""
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    framework_detected = None
    migrations_found = []
    pending = 0
    status = "no_migrations"

    # Check for Alembic (Python/SQLAlchemy)
    alembic_dir = target_path / "alembic"
    if not alembic_dir.exists():
        alembic_dir = target_path / "migrations" / "versions"
    if alembic_dir.exists():
        framework_detected = "alembic"
        for mf in sorted(alembic_dir.glob("*.py")):
            if mf.name == "__init__.py" or mf.name == "env.py":
                continue
            migrations_found.append({
                "file": str(mf.relative_to(target_path)),
                "name": mf.stem,
                "modified": datetime.fromtimestamp(mf.stat().st_mtime, tz=timezone.utc).isoformat(),
            })

    # Check for Django migrations
    django_dirs = list(target_path.rglob("migrations"))
    for md in django_dirs:
        if "node_modules" in str(md) or ".git" in str(md) or "alembic" in str(md):
            continue
        init_file = md / "__init__.py"
        if init_file.exists():
            framework_detected = framework_detected or "django"
            for mf in sorted(md.glob("*.py")):
                if mf.name == "__init__.py":
                    continue
                migrations_found.append({
                    "file": str(mf.relative_to(target_path)),
                    "name": mf.stem,
                    "modified": datetime.fromtimestamp(mf.stat().st_mtime, tz=timezone.utc).isoformat(),
                })

    # Check for Prisma migrations
    prisma_dir = target_path / "prisma" / "migrations"
    if prisma_dir.exists():
        framework_detected = framework_detected or "prisma"
        for mdir in sorted(prisma_dir.iterdir()):
            if mdir.is_dir() and mdir.name != "migration_lock.toml":
                sql_file = mdir / "migration.sql"
                migrations_found.append({
                    "file": str(mdir.relative_to(target_path)),
                    "name": mdir.name,
                    "has_sql": sql_file.exists(),
                    "modified": datetime.fromtimestamp(mdir.stat().st_mtime, tz=timezone.utc).isoformat(),
                })

    # Check for Knex/Sequelize migrations
    knex_dir = target_path / "migrations"
    if knex_dir.exists() and not framework_detected:
        js_migrations = list(knex_dir.glob("*.js")) + list(knex_dir.glob("*.ts"))
        if js_migrations:
            framework_detected = "knex/sequelize"
            for mf in sorted(js_migrations):
                migrations_found.append({
                    "file": str(mf.relative_to(target_path)),
                    "name": mf.stem,
                    "modified": datetime.fromtimestamp(mf.stat().st_mtime, tz=timezone.utc).isoformat(),
                })

    last_applied = migrations_found[-1]["name"] if migrations_found else None
    if migrations_found:
        status = "migrations_found"
        # Without a DB connection, we can only report found migrations, not applied vs pending
        pending = 0  # Would need DB connection to determine

    return {
        "tool": "data.migrate",
        "target": str(target_path),
        "framework_detected": framework_detected,
        "migrations_found": len(migrations_found),
        "migrations": migrations_found,
        "pending_migrations": pending,
        "last_migration": last_applied,
        "status": status,
        "note": "Connect a database to determine applied vs pending migrations." if migrations_found else None,
    }


def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def data_backup(target: str = ".") -> Dict[str, Any]:
    """Back up SQLite and JSON data files to ~/.delimit/backups/."""
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUPS_DIR / timestamp
    _ensure_dir(backup_dir)

    files_backed_up = []
    total_size = 0

    # Collect data files
    data_files = []
    if target_path.is_file():
        data_files = [target_path]
    else:
        for ext in ["*.sqlite", "*.sqlite3", "*.db", "*.json"]:
            for f in target_path.rglob(ext):
                if "node_modules" in str(f) or ".git" in str(f) or str(DELIMIT_HOME) in str(f):
                    continue
                # Skip package.json, tsconfig.json etc -- only back up data files
                if f.name in ("package.json", "package-lock.json", "tsconfig.json", "jsconfig.json", "composer.json"):
                    continue
                data_files.append(f)

    for fpath in data_files:
        try:
            size = fpath.stat().st_size
            if target_path.is_dir():
                rel = fpath.relative_to(target_path)
            else:
                rel = Path(fpath.name)
            dest = backup_dir / rel
            _ensure_dir(dest.parent)
            shutil.copy2(str(fpath), str(dest))
            files_backed_up.append({"file": str(rel), "size_bytes": size})
            total_size += size
        except Exception as e:
            files_backed_up.append({"file": str(fpath.name), "error": str(e)})

    return {
        "tool": "data.backup",
        "target": str(target_path),
        "files_backed_up": len([f for f in files_backed_up if "error" not in f]),
        "files": files_backed_up,
        "backup_path": str(backup_dir),
        "total_size": total_size,
        "total_size_human": _human_size(total_size),
    }


# ═══════════════════════════════════════════════════════════════════════
#  INTEL TOOLS
# ═══════════════════════════════════════════════════════════════════════


def _load_datasets() -> List[Dict[str, Any]]:
    _ensure_dir(INTEL_DIR)
    if DATASETS_FILE.exists():
        try:
            return json.loads(DATASETS_FILE.read_text())
        except Exception:
            return []
    return []


def _save_datasets(datasets: List[Dict[str, Any]]) -> None:
    _ensure_dir(INTEL_DIR)
    DATASETS_FILE.write_text(json.dumps(datasets, indent=2))


def intel_snapshot_ingest(data: Dict[str, Any], provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Save JSON data with provenance metadata."""
    _ensure_dir(SNAPSHOTS_DIR)

    snapshot_id = str(uuid.uuid4())[:12]
    timestamp = datetime.now(timezone.utc).isoformat()

    snapshot = {
        "id": snapshot_id,
        "data": data,
        "provenance": provenance or {},
        "ingested_at": timestamp,
        "checksum": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16],
    }

    snapshot_file = SNAPSHOTS_DIR / f"{snapshot_id}.json"
    snapshot_file.write_text(json.dumps(snapshot, indent=2))

    return {
        "tool": "intel.snapshot_ingest",
        "snapshot_id": snapshot_id,
        "ingested_at": timestamp,
        "checksum": snapshot["checksum"],
        "storage_path": str(snapshot_file),
    }


def _truncate_data(data: Any, max_len: int = 200) -> Any:
    """Truncate data for preview."""
    s = json.dumps(data)
    if len(s) <= max_len:
        return data
    return {"_preview": s[:max_len] + "...", "_truncated": True}


def intel_query(dataset_id: Optional[str] = None, query: str = "", parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Search saved snapshots by keyword/date."""
    _ensure_dir(SNAPSHOTS_DIR)

    results = []
    params = parameters or {}
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    limit = params.get("limit", 50)

    # Search snapshots
    for sf in sorted(SNAPSHOTS_DIR.glob("*.json"), reverse=True):
        try:
            snapshot = json.loads(sf.read_text())
        except Exception:
            continue

        # Filter by dataset_id if specified
        if dataset_id and snapshot.get("provenance", {}).get("dataset_id") != dataset_id:
            continue

        # Date filtering
        ingested = snapshot.get("ingested_at", "")
        if date_from and ingested < date_from:
            continue
        if date_to and ingested > date_to:
            continue

        # Keyword search in data
        if query:
            data_str = json.dumps(snapshot.get("data", {})).lower()
            if query.lower() not in data_str:
                continue

        results.append({
            "snapshot_id": snapshot.get("id"),
            "ingested_at": ingested,
            "provenance": snapshot.get("provenance", {}),
            "data_preview": _truncate_data(snapshot.get("data", {})),
        })

        if len(results) >= limit:
            break

    return {
        "tool": "intel.query",
        "query": query,
        "dataset_id": dataset_id,
        "results": results,
        "total_results": len(results),
    }


def intel_dataset_register(name: str, schema: Optional[Dict[str, Any]] = None,
                           description: Optional[str] = None) -> Dict[str, Any]:
    """Register a new dataset."""
    datasets = _load_datasets()

    # Check for duplicate name
    for ds in datasets:
        if ds.get("name") == name:
            return {"error": "duplicate", "message": f"Dataset '{name}' already registered", "dataset_id": ds["id"]}

    dataset_id = str(uuid.uuid4())[:12]
    new_dataset = {
        "id": dataset_id,
        "name": name,
        "schema": schema or {},
        "description": description or "",
        "frozen": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    datasets.append(new_dataset)
    _save_datasets(datasets)

    return {
        "tool": "intel.dataset_register",
        "dataset": new_dataset,
    }


def intel_dataset_list() -> Dict[str, Any]:
    """List all registered datasets."""
    datasets = _load_datasets()
    return {
        "tool": "intel.dataset_list",
        "datasets": datasets,
        "total": len(datasets),
    }


def intel_dataset_freeze(dataset_id: str) -> Dict[str, Any]:
    """Mark a dataset as immutable."""
    datasets = _load_datasets()

    for ds in datasets:
        if ds.get("id") == dataset_id:
            if ds.get("frozen"):
                return {"tool": "intel.dataset_freeze", "dataset_id": dataset_id, "status": "already_frozen"}
            ds["frozen"] = True
            ds["frozen_at"] = datetime.now(timezone.utc).isoformat()
            ds["updated_at"] = ds["frozen_at"]
            _save_datasets(datasets)
            return {"tool": "intel.dataset_freeze", "dataset_id": dataset_id, "status": "frozen", "frozen_at": ds["frozen_at"]}

    return {"error": "not_found", "message": f"Dataset {dataset_id} not found"}
