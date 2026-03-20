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
    "generic_api_key": r"(?:api[_-]?key|apikey)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}",
    "generic_secret": r"(?:secret|password|passwd|token)\s*[=:]\s*['\"]?[^\s'\"]{8,}",
    "private_key_header": r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----",
    "github_token": r"gh[pousr]_[A-Za-z0-9_]{36,}",
    "slack_token": r"xox[baprs]-[0-9A-Za-z\-]{10,}",
    "jwt_token": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
}

# Dangerous code patterns: name -> (regex, description, severity)
ANTI_PATTERNS = {
    "eval_usage": (r"\beval\s*\(", "Use of eval() — potential code injection", "high"),
    "exec_usage": (r"\bexec\s*\(", "Use of exec() — potential code injection", "high"),
    "sql_concat": (r"""(?:execute|cursor\.execute|query)\s*\(\s*(?:f['\"]|['\"].*%s|.*\+\s*['\"])""", "SQL string concatenation — potential SQL injection", "critical"),
    "dangerous_innerHTML": (r"dangerouslySetInnerHTML", "dangerouslySetInnerHTML — potential XSS", "high"),
    "subprocess_shell": (r"subprocess\.\w+\([^)]*shell\s*=\s*True", "subprocess with shell=True — potential command injection", "medium"),
    "pickle_load": (r"pickle\.loads?\(", "pickle.load — potential arbitrary code execution", "high"),
    "yaml_unsafe_load": (r"yaml\.load\([^)]*(?!Loader)", "yaml.load without safe Loader", "medium"),
    "hardcoded_ip": (r"\b(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b", "Hardcoded internal IP address", "low"),
}

# File extensions to scan
SCAN_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".java", ".rs", ".yaml", ".yml", ".json", ".env", ".sh", ".bash"}

# Skip directories
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".tox", "dist", "build", ".next", ".nuxt", "vendor"}


def _run_cmd(cmd: List[str], timeout: int = 30, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Run a command and return stdout, stderr, returncode."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"Command not found: {cmd[0]}", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Command timed out after {timeout}s", "returncode": -2}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -3}


def _scan_files(target: str) -> List[Path]:
    """Collect scannable source files under target."""
    root = Path(target).resolve()
    files = []
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    for p in root.rglob("*"):
        if any(skip in p.parts for skip in SKIP_DIRS):
            continue
        if p.is_file() and p.suffix in SCAN_EXTENSIONS:
            files.append(p)
        # Cap to avoid scanning massive repos
        if len(files) >= 5000:
            break
    return files


# ─── 5. security_audit ──────────────────────────────────────────────────

def security_audit(target: str = ".") -> Dict[str, Any]:
    """Audit security: dependency vulnerabilities + anti-patterns + secret detection.

    Default: runs pip-audit/npm-audit, regex scans for secrets and dangerous patterns.
    Optional upgrade: set SNYK_TOKEN or TRIVY_PATH for enhanced scanning.
    """
    target_path = Path(target).resolve()
    if not target_path.exists():
        return {"error": "target_not_found", "message": f"Path does not exist: {target}"}

    vulnerabilities = []
    anti_patterns_found = []
    secrets_found = []
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
    files = _scan_files(target)
    tools_used.append(f"pattern-scanner ({len(files)} files)")

    for fpath in files:
        try:
            content = fpath.read_text(errors="ignore")
        except (OSError, PermissionError):
            continue

        rel = str(fpath.relative_to(Path(target).resolve())) if Path(target).resolve() in fpath.parents or fpath == Path(target).resolve() else str(fpath)

        # Secret detection
        for secret_name, pattern in SECRET_PATTERNS.items():
            for match in re.finditer(pattern, content):
                line_num = content[:match.start()].count("\n") + 1
                secrets_found.append({
                    "file": rel,
                    "line": line_num,
                    "type": secret_name,
                    "severity": "critical",
                    "snippet": content[max(0, match.start() - 10):match.end() + 10].strip()[:80],
                })
                severity_counts["critical"] += 1

        # Anti-pattern detection
        for ap_name, (pattern, desc, sev) in ANTI_PATTERNS.items():
            for match in re.finditer(pattern, content):
                line_num = content[:match.start()].count("\n") + 1
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
        r = _run_cmd(["git", "log", f"{last_tag}..HEAD", "--oneline", "--no-decorate"], cwd=cwd)
    else:
        r = _run_cmd(["git", "log", "--oneline", "--no-decorate", "-50"], cwd=cwd)
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


def deploy_site(project_path: str = ".", message: str = "", env_vars: dict = None) -> Dict[str, Any]:
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
            capture_output=True, text=True, timeout=10, cwd=str(p)
        )
        changed_files = [l.strip() for l in status.stdout.strip().splitlines() if l.strip()]
        if not changed_files:
            return {"status": "no_changes", "message": "No changes to deploy."}
        results["changed_files"] = len(changed_files)
        results["steps"].append({"step": "check", "status": "ok", "files": len(changed_files)})
    except Exception as e:
        return {"error": f"Git status failed: {e}"}

    # 2. Git add + commit
    commit_msg = message or "deploy: site update"
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(p), timeout=10, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(p), timeout=10, capture_output=True, text=True
        )
        if result.returncode == 0:
            results["steps"].append({"step": "commit", "status": "ok", "message": commit_msg})
        else:
            results["steps"].append({"step": "commit", "status": "skipped", "detail": "nothing to commit"})
    except Exception as e:
        results["steps"].append({"step": "commit", "status": "error", "detail": str(e)})

    # 3. Git push
    try:
        result = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=str(p), timeout=30, capture_output=True, text=True
        )
        results["steps"].append({
            "step": "push",
            "status": "ok" if result.returncode == 0 else "error",
            "detail": result.stderr.strip()[:200] if result.returncode != 0 else "pushed"
        })
    except Exception as e:
        results["steps"].append({"step": "push", "status": "error", "detail": str(e)})

    # 4. Vercel build
    env = {**os.environ}
    if env_vars:
        env.update(env_vars)

    try:
        result = subprocess.run(
            ["npx", "vercel", "build", "--prod"],
            cwd=str(p), timeout=120, capture_output=True, text=True, env=env
        )
        results["steps"].append({
            "step": "build",
            "status": "ok" if result.returncode == 0 else "error",
            "detail": result.stdout.strip()[-200:] if result.returncode == 0 else result.stderr.strip()[:200]
        })
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

    # 5. Vercel deploy
    try:
        result = subprocess.run(
            ["npx", "vercel", "deploy", "--prebuilt", "--prod"],
            cwd=str(p), timeout=60, capture_output=True, text=True, env=env
        )
        output = result.stdout.strip()
        # Extract deploy URL
        deploy_url = ""
        for line in output.splitlines():
            if "vercel.app" in line or "delimit.ai" in line:
                deploy_url = line.strip()
                break
        results["steps"].append({
            "step": "deploy",
            "status": "ok" if result.returncode == 0 else "error",
            "url": deploy_url
        })
        results["deploy_url"] = deploy_url
    except Exception as e:
        results["steps"].append({"step": "deploy", "status": "error", "detail": str(e)})

    results["status"] = "deployed"
    return results


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

    # 4. Version bump
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
    if dry_run:
        publish_cmd.append("--dry-run")

    try:
        result = subprocess.run(
            publish_cmd, capture_output=True, text=True, timeout=60, cwd=str(p)
        )
        if result.returncode == 0:
            results["steps"].append({
                "step": "publish",
                "status": "ok" if not dry_run else "dry_run",
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

    # 6. Verify on registry (skip for dry run)
    if not dry_run:
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
    if bump in ("patch", "minor", "major") and not dry_run:
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
