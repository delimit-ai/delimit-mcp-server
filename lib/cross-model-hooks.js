#!/usr/bin/env node

/**
 * LED-202: Cross-Model Hook System
 *
 * Detects installed AI coding assistants (Claude Code, Codex, Gemini CLI)
 * and installs Delimit governance hooks into each one's native config format.
 *
 * Hook commands:
 *   delimit hook session-start   -- ledger context + gov health
 *   delimit hook pre-tool <name> -- lint/test checks before edits
 *   delimit hook pre-commit      -- repo diagnostics before commits
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const os = require('os');

// LED-213: Import canonical template for cross-model parity
const { getDelimitSection, getDelimitSectionCondensed } = require('./delimit-template');

// Use process.env.HOME to allow test overrides; fall back to os.homedir()
function getHome() { return process.env.HOME || os.homedir(); }
function getDelimitHome() { return path.join(getHome(), '.delimit'); }

function readJsonl(filePath) {
    if (!fs.existsSync(filePath)) {
        return [];
    }
    return fs.readFileSync(filePath, 'utf-8')
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)
        .map(line => {
            try {
                return JSON.parse(line);
            } catch {
                return null;
            }
        })
        .filter(Boolean);
}

function readLatestSessionSummary(sessionDir) {
    if (!fs.existsSync(sessionDir)) {
        return null;
    }
    const files = fs.readdirSync(sessionDir)
        .filter(name => name.endsWith('.json'))
        .map(name => path.join(sessionDir, name))
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    if (files.length === 0) {
        return null;
    }
    try {
        const data = JSON.parse(fs.readFileSync(files[0], 'utf-8'));
        return {
            id: data.id || path.basename(files[0], '.json'),
            timestamp: data.timestamp || null,
            summary: data.summary || '',
            blockers: Array.isArray(data.blockers) ? data.blockers : [],
            itemsCompleted: Array.isArray(data.items_completed) ? data.items_completed : [],
        };
    } catch {
        return null;
    }
}

function buildLedgerState(ledgerDir) {
    const opsPath = path.join(ledgerDir, 'operations.jsonl');
    const entries = readJsonl(opsPath);
    const latestById = new Map();
    for (const entry of entries) {
        if (!entry || !entry.id) continue;
        const current = latestById.get(entry.id);
        const nextTime = entry.updated_at || entry.created_at || '';
        const currentTime = current ? (current.updated_at || current.created_at || '') : '';
        if (!current || nextTime >= currentTime) {
            latestById.set(entry.id, entry);
        }
    }
    const items = Array.from(latestById.values());
    const open = items
        .filter(item => !['done', 'blocked'].includes(String(item.status || 'open')))
        .sort((a, b) => {
            const prio = { P0: 0, P1: 1, P2: 2 };
            return (prio[a.priority] ?? 9) - (prio[b.priority] ?? 9);
        });
    return {
        items,
        open,
        next: open[0] || null,
    };
}

function getServiceState(serviceName) {
    try {
        const active = execSync(`systemctl is-active ${serviceName} 2>/dev/null`, { encoding: 'utf-8', timeout: 2000 }).trim();
        const enabled = execSync(`systemctl is-enabled ${serviceName} 2>/dev/null`, { encoding: 'utf-8', timeout: 2000 }).trim();
        return { active, enabled };
    } catch {
        const processPattern = serviceName.includes('social')
            ? 'social_daemon.py'
            : serviceName.includes('inbox')
                ? 'inbox_daemon.py'
                : '';
        if (processPattern) {
            try {
                const matches = execSync(`ps -eo pid,cmd | grep ${JSON.stringify(processPattern)} | grep -v grep`, {
                    encoding: 'utf-8',
                    timeout: 2000,
                    stdio: ['ignore', 'pipe', 'ignore'],
                }).trim();
                if (matches) {
                    return { active: 'active', enabled: 'unknown' };
                }
            } catch { /* ignore */ }
        }
        return { active: 'inactive', enabled: 'unknown' };
    }
}

function writeBootstrapState(continuityRoot, payload) {
    fs.mkdirSync(continuityRoot, { recursive: true });
    const statePath = path.join(continuityRoot, 'bootstrap-state.json');
    fs.writeFileSync(statePath, JSON.stringify(payload, null, 2) + '\n');
    return statePath;
}

// ---------------------------------------------------------------------------
// Hook configuration (user-overridable via delimit.yml)
// ---------------------------------------------------------------------------

function loadHookConfig() {
    const defaults = {
        session_start: true,
        pre_tool: true,
        pre_commit: true,
        conditional_hooks: true,
        deploy_audit: true,
        deliberate_on_commit: false,
        show_strategy_items: true,
        // STR-2202 ("tools fire tools") — HOOK half. Both default ENABLED
        // (feature-flags-default-on discipline); additive + reversible.
        session_digest_echo: true,   // SessionStart echoes latest digest + heartbeat anomalies
        agent_record: true,          // PostToolUse flight-recorder for the subagent (Task/Agent) tool
        // LED-1962: SessionStart AUTO-REVIVES the last working soul into the new
        // session (not just a hint to run delimit_revive). This is how a
        // quota-driven agent switch resumes context. Default on; additive + reversible.
        // Scoped to the CURRENT project by default.
        session_auto_revive: true,
        // LED-1962: cross-project fallback is OPT-IN, default OFF. A soul captured
        // under a DIFFERENT project/venture must not bleed into an unrelated
        // (possibly public) repo session by default, since its context could
        // propagate into that session's commits/PRs/transcripts. Enable only for
        // cross-worktree/cross-venture quota-switch continuity (power-user opt-in).
        session_auto_revive_global: false,
    };

    // Check project-level delimit.yml, then global
    const candidates = [
        path.join(process.cwd(), 'delimit.yml'),
        path.join(process.cwd(), '.delimit.yml'),
        path.join(getDelimitHome(), 'delimit.yml'),
    ];

    for (const candidate of candidates) {
        if (fs.existsSync(candidate)) {
            try {
                const yaml = require('js-yaml');
                const doc = yaml.load(fs.readFileSync(candidate, 'utf-8'));  // nosec B-yaml_unsafe_load: parses hook YAML from user-local .claude/
                if (doc && doc.hooks) {
                    return { ...defaults, ...doc.hooks };
                }
            } catch { /* ignore parse errors */ }
        }
    }
    return defaults;
}

// ---------------------------------------------------------------------------
// AI tool detection
// ---------------------------------------------------------------------------

function detectAITools() {
    const detected = [];

    // Claude Code
    const claudeSettings = path.join(getHome(), '.claude', 'settings.json');
    const claudeSettingsLocal = path.join(getHome(), '.claude', 'settings.local.json');
    let hasClaude = fs.existsSync(claudeSettings) || fs.existsSync(claudeSettingsLocal);
    if (!hasClaude) {
        try {
            execSync('claude --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
            hasClaude = true;
        } catch { /* not installed */ }
    }
    if (hasClaude) {
        detected.push({
            id: 'claude',
            name: 'Claude Code',
            configPath: claudeSettings,
            format: 'claude-hooks',
        });
    }

    // Codex CLI
    const codexDir = path.join(getHome(), '.codex');
    let hasCodex = fs.existsSync(codexDir);
    if (!hasCodex) {
        try {
            execSync('codex --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
            hasCodex = true;
        } catch { /* not installed */ }
    }
    if (hasCodex) {
        detected.push({
            id: 'codex',
            name: 'Codex CLI',
            configPath: path.join(codexDir, 'config.json'),
            instructionsPath: path.join(codexDir, 'instructions.md'),
            format: 'codex',
        });
    }

    // Gemini CLI
    const geminiDir = path.join(getHome(), '.gemini');
    let hasGemini = fs.existsSync(geminiDir);
    if (!hasGemini) {
        try {
            execSync('gemini --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
            hasGemini = true;
        } catch { /* not installed */ }
    }
    if (hasGemini) {
        detected.push({
            id: 'gemini',
            name: 'Gemini CLI',
            configPath: path.join(geminiDir, 'settings.json'),
            format: 'gemini-mcp',
        });
    }

    // Antigravity CLI
    const antigravityDir = path.join(getHome(), '.gemini', 'antigravity-cli');
    let hasAntigravity = fs.existsSync(antigravityDir);
    if (!hasAntigravity) {
        try {
            execSync('agy --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
            hasAntigravity = true;
        } catch {
            try {
                execSync('antigravity --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
                hasAntigravity = true;
            } catch { /* not installed */ }
        }
    }
    if (hasAntigravity) {
        detected.push({
            id: 'antigravity',
            name: 'Antigravity CLI',
            configPath: path.join(antigravityDir, 'settings.json'),
            format: 'antigravity-mcp',
        });
    }

    return detected;
}

// ---------------------------------------------------------------------------
// Hook installers per tool
// ---------------------------------------------------------------------------

/**
 * Check if a Claude Code hook group array already contains a delimit hook
 * matching the given command substring.
 */
function findClaudeHookGroup(hookGroups, commandSubstring) {
    if (!Array.isArray(hookGroups)) return null;
    // Match both "npx delimit-cli X" and "delimit-cli X" variants
    const bare = commandSubstring.replace(/^npx /, '');
    for (const group of hookGroups) {
        // Support both nested format (group.hooks[].command) and flat format (group.command)
        if (group.hooks && Array.isArray(group.hooks)) {
            if (group.hooks.some(h => h.command && (h.command.includes(commandSubstring) || h.command.includes(bare)))) {
                return group;
            }
        }
        if (group.command && (group.command.includes(commandSubstring) || group.command.includes(bare))) {
            return group;
        }
    }
    return null;
}

/**
 * Migrate a flat-format hook entry to the nested Claude Code format.
 * Flat: { type, command, matcher, if }
 * Nested: { matcher, if, hooks: [{ type, command }] }
 */
function migrateToNestedFormat(hookGroup) {
    if (hookGroup.hooks && Array.isArray(hookGroup.hooks)) {
        return hookGroup; // Already nested
    }
    const nested = { matcher: hookGroup.matcher || '' };
    if (hookGroup.if) nested.if = hookGroup.if;
    nested.hooks = [{ type: hookGroup.type || 'command', command: hookGroup.command }];
    return nested;
}

/**
 * Install hooks into Claude Code's ~/.claude/settings.json
 *
 * Claude Code hook format (nested):
 *   {
 *     "hooks": {
 *       "EventName": [
 *         {
 *           "matcher": "ToolPattern",
 *           "if": "condition expression",
 *           "hooks": [
 *             { "type": "command", "command": "...", "timeout": 30 }
 *           ]
 *         }
 *       ]
 *     }
 *   }
 *
 * LED-234: Adds conditional hooks that fire only when relevant files change:
 *   1. PostToolUse (Edit|Write) + spec patterns -> delimit lint
 *   2. PreToolUse (Bash) + git commit -> delimit doctor
 *   3. PreToolUse (Bash) + deploy patterns -> delimit security-audit
 */
function installClaudeHooks(tool, hookConfig) {
    // Write to global ~/.claude/settings.json
    const configPath = tool.configPath;
    const configDir = path.dirname(configPath);
    fs.mkdirSync(configDir, { recursive: true });

    // Also write to project .claude/settings.json if the dir exists
    const projectConfigDir = path.join(process.cwd(), '.claude');
    const projectConfigPath = path.join(projectConfigDir, 'settings.json');
    const writeTargets = [configPath];
    if (fs.existsSync(projectConfigDir)) {
        writeTargets.push(projectConfigPath);
    }

    let config = {};
    if (fs.existsSync(configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
        } catch { config = {}; }
    }

    if (!config.hooks) {
        config.hooks = {};
    }

    // Use local binary if installed, fall back to npx
    const { execSync: _exec } = require('child_process');
    let npxCmd;
    try {
        _exec('delimit-cli --version', { stdio: 'pipe', timeout: 3000 });
        npxCmd = 'delimit-cli';
    } catch {
        npxCmd = 'npx delimit-cli';
    }
    const changes = [];

    // --- SessionStart hook ---
    // Write a standalone bash script so it works without npm in PATH
    if (hookConfig.session_start) {
        const home = getHome();
        const hooksDir = path.join(home, '.claude', 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });
        const hookScript = path.join(hooksDir, 'delimit');
        const delimitHome = path.join(home, '.delimit');
        // STR-2202: SessionStart digest + heartbeat echo (HOOK half of "tools
        // fire tools"). Closes the "cron computes, nothing reads" loop — the
        // daily digest already counts stuck dispatches / signals and the
        // heartbeat daemons already write staleness, but nothing surfaced
        // either. Echoes the latest digest one-liner + ONLY heartbeat anomalies
        // into the session's opening context. Best-effort, time-boxed (6s),
        // read-only — NEVER blocks or slows session start (fails open via
        // timeout + '|| true'). Skipped for subagents (they get the scoped
        // one-liner above; this is orchestrator context). Gated by the
        // session_digest_echo config flag (default on; additive + reversible).
        const digestEchoBlock = (hookConfig.session_digest_echo === false) ? '' : (`
if [ "$DELIMIT_SESSION_TYPE" != "subagent" ] && [ "$DELIMIT_SESSION_TYPE" != "agent" ]; then
  DELIMIT_HOME="$DELIMIT_HOME" timeout 6 python3 - <<'DGEOF' 2>/dev/null || true
import json, os, glob, sys
from pathlib import Path
HOME = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit")))
# --- Latest daily digest, one line ---
try:
    dfiles = sorted(glob.glob(str(HOME / "digest" / "digest-*.json")))
    if dfiles:
        newest = dfiles[-1]
        d = json.loads(Path(newest).read_text())
        sig = (d.get("signals") or {}).get("total", 0)
        deb = (d.get("deliberations") or {}).get("total", 0)
        led = (d.get("ledger") or {}).get("opened", 0)
        stuck = (d.get("dispatches") or {}).get("stuck_over_24h", 0)
        date = os.path.basename(newest)[7:17]
        line = "Digest %s: %s signals, %s deliberations, %s new ledger items" % (date, sig, deb, led)
        if stuck:
            line += ", %s stuck dispatches (triage via delimit_agent_dashboard)" % stuck
        print("  " + line)
except Exception:
    pass
# --- Heartbeat anomalies only (silent when green) ---
try:
    sys.path.insert(0, str(HOME / "server"))
    from ai.heartbeat import check_staleness
    res = check_staleness(str(HOME / "heartbeat"))
    bad = [s for s in (res.get("services") or [])
           if s.get("classification") in ("stale", "failed", "parse_error", "never_seen")]
    if bad:
        names = ", ".join("%s(%s)" % (s.get("service"), s.get("classification")) for s in bad[:6])
        print("  Heartbeat: %d daemon(s) need attention: %s" % (len(bad), names))
except Exception:
    pass
DGEOF
fi
`);
        // LED-1962: AUTO-REVIVE the last working soul into the session (not just
        // a hint). SessionStart stdout is injected into the new session, so
        // emitting the prior soul HERE is what makes "switch AI coding agents
        // without losing the plot" actually happen: a fresh Claude Code OR Codex
        // session (e.g. after a quota switch) starts WITH the last
        // task/decisions/next-steps already in context, instead of only being
        // told to go run delimit_revive itself. Imports ai.session_phoenix from
        // the installed server (~/.delimit/server), orchestrator-only, time-boxed
        // (6s), fail-open. Gated by the session_auto_revive config flag (default
        // on; additive + reversible).
        //
        // SCOPE: current project ONLY by default. Cross-project fallback (resume
        // the globally-most-recent soul across ALL projects) is OPT-IN via
        // session_auto_revive_global — otherwise a soul captured under a different
        // project/venture could bleed into an unrelated (possibly public) repo
        // session and propagate into its commits/PRs. The opt-in is passed to the
        // block as the DELIMIT_AUTO_REVIVE_GLOBAL env var ("1" when enabled).
        //
        // No JS interpolation lives inside the RVEOF heredoc — the python f-string
        // uses bare {origin} (no dollar-brace), so it survives into the generated
        // script verbatim. ${autoReviveGlobalEnv} is interpolated only on the bash
        // env-assignment line (outside the quoted heredoc).
        const autoReviveGlobalEnv = (hookConfig.session_auto_revive_global === true) ? '1' : '';
        const autoReviveBlock = (hookConfig.session_auto_revive === false) ? '' : (`
# LED-1962: AUTO-REVIVE the last working soul into the session (not just a hint).
# SessionStart stdout is injected into the new session, so emitting the prior
# soul HERE is what makes "switch AI coding agents without losing the plot"
# actually happen: a fresh Claude Code OR Codex session (e.g. after a quota
# switch) starts WITH the last task/decisions/next-steps already in context,
# instead of only being told to go run delimit_revive itself.
# Current project by default; cross-project resume is opt-in (DELIMIT_AUTO_REVIVE_GLOBAL).
# Orchestrator-only (subagents get scoped handoffs); time-boxed; fail-open.
if [ "$DELIMIT_SESSION_TYPE" != "subagent" ] && [ "$DELIMIT_SESSION_TYPE" != "agent" ]; then
  DELIMIT_HOME="$DELIMIT_HOME" DELIMIT_AUTO_REVIVE_GLOBAL="${autoReviveGlobalEnv}" timeout 6 python3 - <<'RVEOF' 2>/dev/null || true
import os, sys
from pathlib import Path
HOME = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit")))
for _p in (str(HOME / "server"), "/home/delimit/delimit-gateway"):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from ai.session_phoenix import (
        get_latest_soul, find_most_recent_soul_across_projects, _format_revival,
    )
except Exception:
    sys.exit(0)
try:
    cwd = os.getcwd()
    soul = get_latest_soul(cwd)
    origin = cwd
    # Cross-project fallback is OPT-IN: only when session_auto_revive_global is
    # enabled do we resume the globally-most-recent soul (for cross-worktree
    # quota-switch continuity). By default a soul from a DIFFERENT project never
    # bleeds into this session.
    if os.environ.get("DELIMIT_AUTO_REVIVE_GLOBAL") == "1":
        glob_ = find_most_recent_soul_across_projects()
        other = glob_.get("soul") if glob_ else None
        other_path = (glob_ or {}).get("project_path", "")
        if other is not None and (soul is None or (other.created_at or "") > (soul.created_at or "")):
            soul, origin = other, other_path
    if soul is None:
        sys.exit(0)
    print("  === Auto-revived working context (last soul) ===")
    if origin and os.path.realpath(origin) != os.path.realpath(cwd):
        print(f"  (most-recent soul, captured under {origin})")
    print(_format_revival(soul))
except Exception:
    sys.exit(0)
RVEOF
fi
`);
        // Write hook script — use explicit newline prefix to avoid terminal escape contamination
        const scriptContent = '#!/bin/bash\n' + `
# Delimit SessionStart — generated by delimit-cli setup
DELIMIT_HOME="\${DELIMIT_HOME:-${delimitHome}}"

# LED-1705: crash-gap reconciliation. Claude Code passes the SessionStart
# event as JSON on stdin (session_id, transcript_path). If the PRIOR session
# was SIGKILLed (Stop hook never fired -> no .last_capture stamp), salvage a
# cheap deterministic floor handoff from its orphaned transcript. The newest
# transcript at session start is the CURRENT session's, so we exclude it and
# salvage the next-most-recent. Time-boxed, best-effort, never blocks start.
SESSIONSTART_EVENT_JSON="$(cat 2>/dev/null || true)"
DELIMIT_HOME="$DELIMIT_HOME" SESSIONSTART_EVENT_JSON="$SESSIONSTART_EVENT_JSON" \\
  timeout 8 python3 - <<'PYEOF' 2>/dev/null || true
import json, os, sys, time, subprocess
from pathlib import Path

HOME = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit")))
stamp_path = HOME / ".last_capture"

# Double-reconcile / clean-exit guard: any stamp means the prior session was
# already captured (cleanly OR by an earlier reconcile) -- nothing to salvage.
try:
    if stamp_path.exists():
        json.loads(stamp_path.read_text())
        sys.exit(0)
except Exception:
    pass

# Parse SessionStart event for the CURRENT transcript path (to exclude it).
cur_transcript = ""
try:
    ev = json.loads(os.environ.get("SESSIONSTART_EVENT_JSON", "") or "{}")
    cur_transcript = ev.get("transcript_path", "") or ""
except Exception:
    pass
if not cur_transcript:
    sys.exit(0)

# Find the newest sibling transcript that isn't the current session's.
try:
    cur = Path(cur_transcript)
    proj_dir = cur.parent
    cur_real = os.path.realpath(str(cur))
    cands = [f for f in proj_dir.glob("*.jsonl") if os.path.realpath(str(f)) != cur_real]
    if not cands:
        sys.exit(0)
    orphan = str(max(cands, key=lambda f: f.stat().st_mtime))
except Exception:
    sys.exit(0)

def run_git(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=4)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

# Cheap transcript-tail parse (no LLM): last assistant text + tool names.
# LED-1713: robust to thinking-tails — a mid-work session end often leaves only
# thinking + tool_use in the last lines (no text block). Prefer the last text
# block; fall back to the last thinking block ("[thinking] ") so the floor
# handoff is never "(no final assistant text)". Widen (capped) to recover a
# real text block pushed out of the immediate tail.
final_text, final_thinking, tool_calls, turns = "", "", [], 0
def _rc(o):
    m = o.get("message") if isinstance(o, dict) else None
    if isinstance(m, dict):
        return (m.get("role", "") or (o.get("type", "") if isinstance(o, dict) else "")), m.get("content")
    return (o.get("type", "") if isinstance(o, dict) else ""), (o.get("content") if isinstance(o, dict) else None)
def _ex(content, sink):
    tx, th = [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_use" and b.get("name") and sink is not None:
                sink.append(str(b["name"]))
            elif bt == "text" and b.get("text"):
                tx.append(str(b["text"]))
            elif bt == "thinking" and b.get("thinking"):
                th.append(str(b["thinking"]))
    elif isinstance(content, str):
        tx.append(content)
    return "\\n".join(tx).strip(), "\\n".join(th).strip()
def _tail_text(path):
    # Read only the trailing ~64KB (seek-from-end): transcripts can be MB and
    # this is a time-boxed hook. Drop the partial first line when mid-file.
    # Best-effort: fall back to a full read so correctness never regresses.
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - 65536)
            fh.seek(start)
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
        if start > 0:
            nl = text.find("\\n")
            text = text[nl + 1:] if nl != -1 else ""
        return text
    except Exception:
        return Path(path).read_text(errors="replace")
try:
    tl = [l for l in _tail_text(orphan).splitlines() if l.strip()]
    tail = tl[-10:]
    turns = len(tail)
    for raw in tail:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        role, content = _rc(obj)
        tx, th = _ex(content, tool_calls)
        if role == "assistant":
            if tx:
                final_text = tx
            if th:
                final_thinking = th
    if not final_text and len(tl) > len(tail):
        for raw in tl[-40:]:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            role, content = _rc(obj)
            if role != "assistant":
                continue
            tx, th = _ex(content, None)
            if tx:
                final_text = tx
            if th:
                final_thinking = th
    if not final_text and final_thinking:
        final_text = "[thinking] " + final_thinking
except Exception:
    pass

if not turns:
    sys.exit(0)

branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
status = run_git(["status", "--porcelain"])
changed = [l[3:].strip() for l in status.splitlines() if l.strip()][:50] if status else []

sessions = HOME / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
sid = "session_" + time.strftime("%Y%m%d_%H%M%S")
summary = ("[deterministic floor / orphaned] " + (final_text or "(no final assistant text)"))[:2000]
key_decisions = []
if tool_calls:
    key_decisions.append("Last tool activity: " + ", ".join(tool_calls[-10:]))
handoff = {
    "id": sid,
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "venture": "all",
    "summary": summary,
    "items_completed": [],
    "items_added": [],
    "key_decisions": key_decisions,
    "blockers": [],
    "files_changed": changed,
    "source": "deterministic",
    "quality": "floor",
    "git_branch": branch,
    "salvaged_from": os.path.basename(orphan),
}
try:
    (sessions / (sid + ".json")).write_text(json.dumps(handoff, indent=2))
except Exception:
    pass

# Stamp so a second session start won't re-salvage the same orphan.
try:
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps({
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": sid,
        "source": "deterministic",
        "quality": "floor",
    }))
except Exception:
    pass
PYEOF

echo "  <"
echo "  === Delimit Status ==="
# Governance
if [ -f "./delimit.yml" ] || [ -f "./.delimit/policies.yml" ]; then
  echo "Governance: active | policy=project"
elif [ -f "$DELIMIT_HOME/delimit.yml" ]; then
  echo "Governance: active | policy=user"
else
  echo "Governance: not initialized -- run npx delimit-cli init"
fi
# Server + tools
SERVER="$DELIMIT_HOME/server/ai/server.py"
if [ -f "$SERVER" ]; then
  TOOLS=$(grep -c '@mcp.tool' "$SERVER" 2>/dev/null || echo "0")
  echo "Server: ready ($TOOLS tools)"
else
  echo "Server: not installed -- run npx delimit-cli setup"
fi
# Hooks + audit
[ -f "${home}/.claude/settings.json" ] && grep -q '"hooks"' "${home}/.claude/settings.json" 2>/dev/null && HOOKS="enabled" || HOOKS="disabled"
[ -d "$DELIMIT_HOME/audit" ] && AUDIT="on" || AUDIT="off"
echo "Hooks: $HOOKS | Audit: $AUDIT"
# MCP
[ -f "${home}/.mcp.json" ] && grep -q "delimit" "${home}/.mcp.json" 2>/dev/null && echo "MCP: delimit registered" || echo "MCP: not registered"
# Models
MODELS=""
[ -n "$XAI_API_KEY" ] && MODELS="\${MODELS}Grok + "
[ -n "$GOOGLE_APPLICATION_CREDENTIALS" ] && MODELS="\${MODELS}Gemini + "
[ -n "$OPENAI_API_KEY" ] && MODELS="\${MODELS}Codex + "
[ -f "$DELIMIT_HOME/models.json" ] && MODELS=$(python3 -c "import json; d=json.load(open('$DELIMIT_HOME/models.json')); print(' + '.join(v.get('name',k) for k,v in d.items() if v.get('enabled')))" 2>/dev/null) || true
[ -n "$MODELS" ] && echo "Deliberation: \${MODELS% + }"
# Recent sessions (rolling window — orchestrator startup digest)
# Subagent sessions get a scoped one-liner instead of the full digest, to
# avoid pulling the orchestrator's multi-session context into a narrow task.
SESSIONS="$DELIMIT_HOME/sessions"
if [ "$DELIMIT_SESSION_TYPE" = "subagent" ] || [ "$DELIMIT_SESSION_TYPE" = "agent" ]; then
  echo "Session: subagent (scoped) | handoff=\${DELIMIT_HANDOFF_ID:-none}"
  echo "Revive scoped context with: delimit_revive(scope=\\"\${DELIMIT_HANDOFF_ID:-<handoff_id>}\\")"
elif [ -d "$SESSIONS" ]; then
  echo "Recent sessions (revive full state via delimit_revive + delimit_session_history):"
  ls -t "$SESSIONS"/session_*.json 2>/dev/null | head -8 | while read -r f; do
    python3 -c "import json; d=json.load(open('$f')); print('  - '+d.get('timestamp','?')[:10]+' '+d.get('summary','(no summary)')[:160])" 2>/dev/null
  done
fi
${autoReviveBlock}
${digestEchoBlock}
# LED-1710: receiving-agent handoff post-flight. When a fresh session starts in a
# git repo, re-validate the cross-agent handoff invariants the SENDING agent was
# supposed to leave clean (junk identity / bare repo / stale index.lock / leaked
# GIT_*). Read-only; surfaces only CRITICAL corruptions (so warn-level noise like
# a stale capture stamp doesn't clutter every start). Time-boxed; never blocks.
PF_BASE="$DELIMIT_HOME/server"
if [ -f "$PF_BASE/ai/handoff_preflight.py" ] && [ -d "./.git" ]; then
  PF_BASE="$PF_BASE" timeout 5 python3 - <<'PFEOF' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ.get("PF_BASE", ""))
try:
    from ai.handoff_preflight import preflight_check
    res = preflight_check(os.getcwd())
except Exception:
    sys.exit(0)
bad = [c for c in res.get("checks", []) if c and not c.get("ok")]
crit = [c for c in bad if c.get("severity") == "critical"]
if crit:
    print("Handoff post-flight: %d cross-agent issue(s) in this repo:" % len(crit))
    for c in crit:
        print("  ! %s: %s" % (c.get("name"), c.get("detail")))
        if c.get("remediation"):
            print("      fix: %s" % c.get("remediation"))
PFEOF
fi
echo "  === Delimit Ready ==="
# Note: shim governance works via PATH ordering ($HOME/.delimit/shims first).
# We deliberately do NOT mv claude → claude-real or copy the shim into /usr/bin/claude.
# That race-prone workaround caused "[Delimit] claude not found in PATH" failures
# when npm reinstalls clobbered /usr/bin/claude mid-operation.
`;
        fs.writeFileSync(hookScript, scriptContent);
        fs.chmodSync(hookScript, '755');

        if (!config.hooks.SessionStart) {
            config.hooks.SessionStart = [];
        }
        // Check if identical hook already exists
        const existingSession = config.hooks.SessionStart.find(group => {
            const cmds = (group.hooks || []).map(h => h.command || '');
            return cmds.some(c => c === hookScript);
        });
        if (!existingSession) {
            // Remove any old delimit hooks (both script and npm command variants)
            config.hooks.SessionStart = config.hooks.SessionStart.filter(group => {
                const cmds = (group.hooks || []).map(h => h.command || '');
                return !cmds.some(c => c.includes('delimit'));
            });
            config.hooks.SessionStart.push({
                matcher: '',
                hooks: [{
                    type: 'command',
                    command: hookScript,
                    timeout: 10,
                }],
            });
            changes.push('SessionStart');
        }
    }

    // --- PreToolUse: pre-tool hook scoped to Edit/Write on spec files ---
    if (hookConfig.pre_tool) {
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const existing = findClaudeHookGroup(config.hooks.PreToolUse, 'delimit-cli hook pre-tool');
        if (existing) {
            // Upgrade flat-format hook to nested + add if condition if missing
            const migrated = migrateToNestedFormat(existing);
            if (!migrated.if) {
                const idx = config.hooks.PreToolUse.indexOf(existing);
                migrated.matcher = 'Edit|Write';
                migrated.if = "Edit && (path_matches('**/openapi*') || path_matches('**/swagger*') || path_matches('**/*.yaml') || path_matches('**/*.yml'))";
                migrated.hooks = [{ type: 'command', command: `${npxCmd} hook pre-tool $TOOL_NAME` }];
                config.hooks.PreToolUse[idx] = migrated;
                changes.push('PreToolUse (upgraded)');
            }
        } else {
            config.hooks.PreToolUse.push({
                matcher: 'Edit|Write',
                if: "Edit && (path_matches('**/openapi*') || path_matches('**/swagger*') || path_matches('**/*.yaml') || path_matches('**/*.yml'))",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} hook pre-tool $TOOL_NAME`,
                }],
            });
            changes.push('PreToolUse');
        }
    }

    // --- PreToolUse: pre-commit governance on git commit/push ---
    if (hookConfig.pre_commit) {
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const existing = findClaudeHookGroup(config.hooks.PreToolUse, 'delimit-cli hook pre-commit');
        if (!existing) {
            config.hooks.PreToolUse.push({
                matcher: 'Bash',
                if: "Bash && (input_contains('git commit') || input_contains('git push'))",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} hook pre-commit`,
                }],
            });
            changes.push('PreCommit');
        }
    }

    // --- LED-234: Conditional hooks (opt-in via conditional_hooks config) ---
    if (hookConfig.conditional_hooks !== false) {

        // 1. PostToolUse: auto-lint after editing OpenAPI spec files
        if (!config.hooks.PostToolUse) {
            config.hooks.PostToolUse = [];
        }
        const specLintCmd = 'delimit-cli lint';
        const existingSpecLint = findClaudeHookGroup(config.hooks.PostToolUse, specLintCmd);
        if (!existingSpecLint) {
            config.hooks.PostToolUse.push({
                matcher: 'Edit|Write',
                if: "path_matches('**/openapi*.yaml') || path_matches('**/openapi*.yml') || path_matches('**/openapi*.json') || path_matches('**/swagger*.yaml') || path_matches('**/swagger*.yml') || path_matches('**/swagger*.json')",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} lint "$DELIMIT_FILE_PATH"`,
                    timeout: 30,
                }],
            });
            changes.push('PostToolUse:spec-lint');
        }

        // 2. PreToolUse: repo diagnose before git commit (uses doctor command)
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const doctorCmd = 'delimit-cli doctor';
        const existingDoctor = findClaudeHookGroup(config.hooks.PreToolUse, doctorCmd);
        if (!existingDoctor) {
            config.hooks.PreToolUse.push({
                matcher: 'Bash',
                if: "command matches 'git commit'",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} doctor`,
                    timeout: 15,
                }],
            });
            changes.push('PreToolUse:doctor');
        }

        // 3. PreToolUse: security audit before deploy/publish/release commands
        if (hookConfig.deploy_audit !== false) {
            const deployGateCmd = 'delimit-cli hook deploy-gate';
            const existingSecurity = findClaudeHookGroup(config.hooks.PreToolUse, deployGateCmd);
            if (!existingSecurity) {
                config.hooks.PreToolUse.push({
                    matcher: 'Bash',
                    if: "command matches 'npm publish' or command matches 'npx deploy' or command matches 'deploy' or command matches 'release' or command matches 'docker compose up' or command matches 'docker-compose up' or command matches 'docker build'",
                    hooks: [{
                        type: 'command',
                        command: `${npxCmd} hook deploy-gate`,
                        timeout: 30,
                    }],
                });
                changes.push('PreToolUse:deploy-gate');
            }
        }
    }

    // --- Stop hook: session handoff on exit ---
    if (hookConfig.session_start) {  // If session-start is enabled, also add session-end
        if (!config.hooks.Stop) {
            config.hooks.Stop = [];
        }
        const home = getHome();
        const hooksDir = path.join(home, '.claude', 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });
        const stopScript = path.join(hooksDir, 'delimit-stop');
        const delimitHome = path.join(home, '.delimit');
        const stopContent = '#!/bin/bash\n' + `
# Delimit Stop — the > in </>
# LED-1705: deterministic session-end capture floor.
#
# Claude Code passes the Stop event as JSON on stdin (includes
# transcript_path). We:
#   1. Preserve the existing ledger git push + timestamp behavior.
#   2. If a fresh (<5 min) model-invoked capture exists, SKIP the floor so we
#      don't clobber the richer artifact.
#   3. Otherwise write a CHEAP deterministic floor handoff (git state +
#      ledger context + transcript tail — NO LLM call) and stamp
#      .last_capture so the next revive knows the session ended cleanly.
# Must stay under the hook's 10s budget.

DELIMIT_HOME="\${DELIMIT_HOME:-${delimitHome}}"
LEDGER_DIR="$DELIMIT_HOME/ledger"

# Capture stdin (Stop event JSON) so the python floor can read transcript_path.
STOP_EVENT_JSON="$(cat 2>/dev/null || true)"

# Push ledger changes so other models pick them up
if [ -d "$LEDGER_DIR/.git" ]; then
  cd "$LEDGER_DIR"
  git add -A 2>/dev/null
  git commit -m "session handoff $(date -u +%Y-%m-%dT%H:%M:%SZ)" --no-verify 2>/dev/null
  git push origin main 2>/dev/null &
fi

# Save session timestamp (back-compat)
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DELIMIT_HOME/.last_session_end"

# --- Deterministic capture floor (best-effort, time-boxed) ---
CAPTURE_RESULT="$(DELIMIT_HOME="$DELIMIT_HOME" STOP_EVENT_JSON="$STOP_EVENT_JSON" \\
  timeout 8 python3 - <<'PYEOF' 2>/dev/null || true
import json, os, sys, time, subprocess
from pathlib import Path

HOME = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit")))
FRESH = 5 * 60
stamp_path = HOME / ".last_capture"

def read_stamp():
    try:
        return json.loads(stamp_path.read_text())
    except Exception:
        return None

# Freshness gate: a fresh model capture wins — skip the floor.
stamp = read_stamp()
if stamp and stamp.get("source") == "model":
    try:
        if (time.time() - float(stamp.get("ts", 0))) <= FRESH:
            print("skip:fresh-model-capture")
            sys.exit(0)
    except Exception:
        pass

# Parse the Stop event JSON for the transcript path.
transcript_path = ""
try:
    ev = json.loads(os.environ.get("STOP_EVENT_JSON", "") or "{}")
    transcript_path = ev.get("transcript_path", "") or ""
except Exception:
    pass

def run_git(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=4)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
status = run_git(["status", "--porcelain"])
changed = [l[3:].strip() for l in status.splitlines() if l.strip()][:50] if status else []
recent_commits = run_git(["log", "-3", "--oneline"]).splitlines() if branch else []

# Cheap ledger context: newest few operations.jsonl summaries.
ledger_items = []
try:
    ops = HOME / "ledger" / "operations.jsonl"
    if ops.exists():
        lines = [l for l in ops.read_text(errors="replace").splitlines() if l.strip()]
        for raw in lines[-40:]:
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if o.get("type") == "update":
                continue
            t = o.get("title") or o.get("summary") or ""
            if t:
                ledger_items.append(t[:120])
        ledger_items = ledger_items[-5:]
except Exception:
    pass

# Transcript tail: last assistant text + tool-call names (no LLM).
# LED-1713: robust to thinking-tails — prefer the last text block, fall back to
# the last thinking block ("[thinking] ") so the floor is never empty, and
# widen (capped) to recover a text block pushed out of the immediate tail.
final_text, final_thinking, tool_calls, turns = "", "", [], 0
def _rc(o):
    m = o.get("message") if isinstance(o, dict) else None
    if isinstance(m, dict):
        return (m.get("role", "") or (o.get("type", "") if isinstance(o, dict) else "")), m.get("content")
    return (o.get("type", "") if isinstance(o, dict) else ""), (o.get("content") if isinstance(o, dict) else None)
def _ex(content, sink):
    tx, th = [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_use" and b.get("name") and sink is not None:
                sink.append(str(b["name"]))
            elif bt == "text" and b.get("text"):
                tx.append(str(b["text"]))
            elif bt == "thinking" and b.get("thinking"):
                th.append(str(b["thinking"]))
    elif isinstance(content, str):
        tx.append(content)
    return "\\n".join(tx).strip(), "\\n".join(th).strip()
def _tail_text(path):
    # Read only the trailing ~64KB (seek-from-end): transcripts can be MB and
    # this is a time-boxed hook. Drop the partial first line when mid-file.
    # Best-effort: fall back to a full read so correctness never regresses.
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - 65536)
            fh.seek(start)
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
        if start > 0:
            nl = text.find("\\n")
            text = text[nl + 1:] if nl != -1 else ""
        return text
    except Exception:
        return Path(path).read_text(errors="replace")
try:
    if transcript_path and Path(transcript_path).exists():
        tl = [l for l in _tail_text(transcript_path).splitlines() if l.strip()]
        tail = tl[-10:]
        turns = len(tail)
        for raw in tail:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            role, content = _rc(obj)
            tx, th = _ex(content, tool_calls)
            if role == "assistant":
                if tx:
                    final_text = tx
                if th:
                    final_thinking = th
        if not final_text and len(tl) > len(tail):
            for raw in tl[-40:]:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                role, content = _rc(obj)
                if role != "assistant":
                    continue
                tx, th = _ex(content, None)
                if tx:
                    final_text = tx
                if th:
                    final_thinking = th
        if not final_text and final_thinking:
            final_text = "[thinking] " + final_thinking
except Exception:
    pass

# Write the floor handoff into the sessions dir (same shape as session_handoff).
sessions = HOME / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
sid = "session_" + time.strftime("%Y%m%d_%H%M%S")
summary = ("[deterministic floor] " + (final_text or "(no final assistant text)"))[:2000]
key_decisions = []
if tool_calls:
    key_decisions.append("Last tools: " + ", ".join(tool_calls[-10:]))
if ledger_items:
    key_decisions.append("Open ledger: " + " | ".join(ledger_items))
if recent_commits:
    key_decisions.append("Recent commits: " + " | ".join(recent_commits))
handoff = {
    "id": sid,
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "venture": "all",
    "summary": summary,
    "items_completed": [],
    "items_added": [],
    "key_decisions": key_decisions,
    "blockers": [],
    "files_changed": changed,
    "source": "deterministic",
    "quality": "floor",
    "git_branch": branch,
}
try:
    (sessions / (sid + ".json")).write_text(json.dumps(handoff, indent=2))
except Exception:
    pass

# Stamp .last_capture so the next revive sees a clean exit.
try:
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps({
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": sid,
        "source": "deterministic",
        "quality": "floor",
    }))
except Exception:
    pass

print("floor:" + sid)
PYEOF
)"

echo ""
echo "  />"
case "$CAPTURE_RESULT" in
  skip:*)  echo "  Fresh capture already saved this session — floor skipped." ;;
  floor:*) echo "  Deterministic session floor captured (git + ledger + transcript tail)." ;;
  *)       echo "  Session timestamp saved (floor capture unavailable)." ;;
esac
echo ""
`;
        fs.writeFileSync(stopScript, stopContent);
        fs.chmodSync(stopScript, '755');

        const existingStop = config.hooks.Stop.find(group => {
            const cmds = (group.hooks || []).map(h => h.command || '');
            return cmds.some(c => c.includes('delimit'));
        });
        if (!existingStop) {
            config.hooks.Stop.push({
                matcher: '',
                hooks: [{
                    type: 'command',
                    command: stopScript,
                    timeout: 10,
                }],
            });
            changes.push('Stop');
        }
    }

    // --- SessionEnd hook: deterministic capture floor on real session exit ---
    //
    // Bug this fixes: the Stop event fires when the assistant FINISHES A TURN,
    // not when the user actually leaves. On a real exit (/exit, window close,
    // SIGHUP) Claude Code fires SessionEnd — and we were never installing a
    // SessionEnd hook (or, in older installs, one written in the FLAT shape that
    // Claude Code silently ignores). Result: on exit nothing captured a handoff
    // and the user had to manually prompt delimit_soul_capture.
    //
    // CRITICAL SHAPE CONTRACT: Claude Code only executes hooks in the NESTED
    // shape — { matcher, hooks: [{ type, command, timeout }] }. A FLAT entry
    // ({ type, command, matcher } at the top level) is parsed but never run.
    // We ALWAYS write the nested shape here (same as SessionStart / Stop) so the
    // hook actually fires for every installed user.
    //
    // Gated on session_start like the Stop hook (session lifecycle). The
    // deployed delimit-session-end script does a DETERMINISTIC, time-boxed
    // (<10s), NO-LLM capture: a session soul FLOOR (git state + ledger context
    // + transcript tail from the SessionEnd event's transcript_path), stamped to
    // .last_capture so the next session's delimit_revive finds a clean
    // end-of-session handoff. A rich LLM soul still requires the model; this
    // floor is the guaranteed automatic baseline. Best-effort, never blocks exit.
    if (hookConfig.session_start) {
        if (!config.hooks.SessionEnd) {
            config.hooks.SessionEnd = [];
        }
        const home = getHome();
        const hooksDir = path.join(home, '.claude', 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });
        const sessionEndScript = path.join(hooksDir, 'delimit-session-end');
        const delimitHome = path.join(home, '.delimit');
        const sessionEndContent = '#!/bin/bash\n' + `
# Delimit SessionEnd — the guaranteed > in </> on real session exit.
#
# Claude Code fires SessionEnd on /exit, logout, window close, or clear, and
# passes the event as JSON on stdin (session_id, transcript_path, reason). The
# Stop hook only fires at end-of-TURN, so it can miss a hard exit; SessionEnd is
# the safety net that guarantees an end-of-session floor handoff exists.
#
# This is a DETERMINISTIC, NO-LLM, time-boxed (<10s) capture:
#   * git state (branch + porcelain changes + recent commits)
#   * cheap ledger context (newest operations.jsonl summaries)
#   * transcript tail (last assistant text + tool names — NO model call)
# It writes a session soul FLOOR into ~/.delimit/sessions and stamps
# .last_capture so the next session's delimit_revive sees a clean exit.
# A rich LLM soul still needs the model (delimit_soul_capture); this floor is
# the automatic baseline. Best-effort — ALWAYS exits 0, never blocks exit.

DELIMIT_HOME="\${DELIMIT_HOME:-${delimitHome}}"

# Capture stdin (SessionEnd event JSON) so the python floor can read transcript_path.
SESSIONEND_EVENT_JSON="$(cat 2>/dev/null || true)"

# Save session timestamp (back-compat, mirrors the Stop hook).
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DELIMIT_HOME/.last_session_end" 2>/dev/null || true

DELIMIT_HOME="$DELIMIT_HOME" SESSIONEND_EVENT_JSON="$SESSIONEND_EVENT_JSON" \\
  timeout 8 python3 - <<'PYEOF' 2>/dev/null || true
import json, os, sys, time, subprocess
from pathlib import Path

HOME = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit")))
FRESH = 5 * 60
stamp_path = HOME / ".last_capture"

# Freshness gate: a fresh MODEL capture (a rich soul the user explicitly made)
# wins — never clobber it with a floor. A prior deterministic floor does NOT
# block us: SessionEnd should record the true end-of-session state.
try:
    stamp = json.loads(stamp_path.read_text())
    if stamp.get("source") == "model" and (time.time() - float(stamp.get("ts", 0))) <= FRESH:
        sys.exit(0)
except Exception:
    pass

# Parse the SessionEnd event JSON for the transcript path.
transcript_path = ""
try:
    ev = json.loads(os.environ.get("SESSIONEND_EVENT_JSON", "") or "{}")
    transcript_path = ev.get("transcript_path", "") or ""
except Exception:
    pass

def run_git(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=4)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
status = run_git(["status", "--porcelain"])
changed = [l[3:].strip() for l in status.splitlines() if l.strip()][:50] if status else []
recent_commits = run_git(["log", "-3", "--oneline"]).splitlines() if branch else []

# Cheap ledger context: newest few operations.jsonl summaries.
ledger_items = []
try:
    ops = HOME / "ledger" / "operations.jsonl"
    if ops.exists():
        lines = [l for l in ops.read_text(errors="replace").splitlines() if l.strip()]
        for raw in lines[-40:]:
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if o.get("type") == "update":
                continue
            t = o.get("title") or o.get("summary") or ""
            if t:
                ledger_items.append(t[:120])
        ledger_items = ledger_items[-5:]
except Exception:
    pass

# Transcript tail: last assistant text + tool-call names (no LLM). Robust to
# thinking-tails — prefer the last text block, fall back to the last thinking
# block ("[thinking] ") so the floor is never empty; widen (capped) to recover
# a text block pushed out of the immediate tail. Reads only the trailing ~64KB.
final_text, final_thinking, tool_calls, turns = "", "", [], 0
def _rc(o):
    m = o.get("message") if isinstance(o, dict) else None
    if isinstance(m, dict):
        return (m.get("role", "") or (o.get("type", "") if isinstance(o, dict) else "")), m.get("content")
    return (o.get("type", "") if isinstance(o, dict) else ""), (o.get("content") if isinstance(o, dict) else None)
def _ex(content, sink):
    tx, th = [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_use" and b.get("name") and sink is not None:
                sink.append(str(b["name"]))
            elif bt == "text" and b.get("text"):
                tx.append(str(b["text"]))
            elif bt == "thinking" and b.get("thinking"):
                th.append(str(b["thinking"]))
    elif isinstance(content, str):
        tx.append(content)
    return "\\n".join(tx).strip(), "\\n".join(th).strip()
def _tail_text(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - 65536)
            fh.seek(start)
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
        if start > 0:
            nl = text.find("\\n")
            text = text[nl + 1:] if nl != -1 else ""
        return text
    except Exception:
        return Path(path).read_text(errors="replace")
try:
    if transcript_path and Path(transcript_path).exists():
        tl = [l for l in _tail_text(transcript_path).splitlines() if l.strip()]
        tail = tl[-10:]
        turns = len(tail)
        for raw in tail:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            role, content = _rc(obj)
            tx, th = _ex(content, tool_calls)
            if role == "assistant":
                if tx:
                    final_text = tx
                if th:
                    final_thinking = th
        if not final_text and len(tl) > len(tail):
            for raw in tl[-40:]:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                role, content = _rc(obj)
                if role != "assistant":
                    continue
                tx, th = _ex(content, None)
                if tx:
                    final_text = tx
                if th:
                    final_thinking = th
        if not final_text and final_thinking:
            final_text = "[thinking] " + final_thinking
except Exception:
    pass

# Write the floor handoff into the sessions dir (same shape as session_handoff).
sessions = HOME / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
sid = "session_" + time.strftime("%Y%m%d_%H%M%S")
summary = ("[deterministic floor / session-end] " + (final_text or "(no final assistant text)"))[:2000]
key_decisions = []
if tool_calls:
    key_decisions.append("Last tools: " + ", ".join(tool_calls[-10:]))
if ledger_items:
    key_decisions.append("Open ledger: " + " | ".join(ledger_items))
if recent_commits:
    key_decisions.append("Recent commits: " + " | ".join(recent_commits))
handoff = {
    "id": sid,
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "venture": "all",
    "summary": summary,
    "items_completed": [],
    "items_added": [],
    "key_decisions": key_decisions,
    "blockers": [],
    "files_changed": changed,
    "source": "deterministic",
    "quality": "floor",
    "git_branch": branch,
    "trigger": "session-end",
}
try:
    (sessions / (sid + ".json")).write_text(json.dumps(handoff, indent=2))
except Exception:
    pass

# Stamp .last_capture so the next revive sees a clean exit.
try:
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps({
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": sid,
        "source": "deterministic",
        "quality": "floor",
        "trigger": "session-end",
    }))
except Exception:
    pass
PYEOF
exit 0
`;
        fs.writeFileSync(sessionEndScript, sessionEndContent);
        fs.chmodSync(sessionEndScript, '755');

        // Idempotent + no-clobber: only add our nested-shape group if no delimit
        // SessionEnd hook already exists. User-owned SessionEnd hooks (any command
        // without 'delimit') are left untouched — we merge, never replace.
        const existingSessionEnd = config.hooks.SessionEnd.find(group => {
            const cmds = (group.hooks || []).map(h => h.command || '');
            return cmds.some(c => c.includes('delimit'));
        });
        if (!existingSessionEnd) {
            config.hooks.SessionEnd.push({
                matcher: '',
                hooks: [{
                    type: 'command',
                    command: sessionEndScript,
                    timeout: 10,
                }],
            });
            changes.push('SessionEnd');
        }
    }

    // --- PostToolUse: STR-2202 subagent flight-recorder ---
    // A PostToolUse hook matched to the subagent-spawn tool (Claude Code names
    // it "Task"; some harnesses "Agent") auto-records the dispatch AND its
    // completion. Because PostToolUse fires AFTER the matched tool returns, a
    // single invocation sees both the spawn's input (description / prompt /
    // subagent_type) and its result (tool_response) — so the record is filled
    // by mechanism, not by the orchestrator remembering to call agent_dispatch.
    // Gated on session_start (lifecycle instrumentation, like the Stop hook)
    // AND the agent_record flag (default on). Additive + reversible: uninstall
    // strips it via removeClaudeHooks' delimit-command filter.
    if (hookConfig.session_start && hookConfig.agent_record !== false) {
        const home = getHome();
        const hooksDir = path.join(home, '.claude', 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });
        const recorderScript = path.join(hooksDir, 'delimit-agent-record');
        const delimitHome = path.join(home, '.delimit');
        const recorderContent = '#!/bin/bash\n' + `
# Delimit PostToolUse flight-recorder (STR-2202 — HOOK half of "tools fire tools").
#
# Claude Code invokes PostToolUse hooks AFTER the matched tool completes, passing
# the event as JSON on stdin: {tool_name, tool_input, tool_response, cwd,
# session_id, transcript_path}. For the subagent tool (Task/Agent), tool_input
# carries {description, prompt, subagent_type} and tool_response carries the
# subagent's result. Because the hook fires post-completion, one invocation
# records BOTH the dispatch and its completion — the AGT flight recorder is
# filled by the airplane, not the pilot's diary.
#
# The record carries {model, task_type, outcome, venture} so delegated-work
# outcomes feed the LED-3720 instrumentation. Best-effort, time-boxed, ALWAYS
# exits 0 — a recorder must never disrupt the tool it observes.
DELIMIT_HOME="\${DELIMIT_HOME:-${delimitHome}}"
EVENT_JSON="$(cat 2>/dev/null || true)"

DELIMIT_HOME="$DELIMIT_HOME" EVENT_JSON="$EVENT_JSON" \\
  timeout 6 python3 - <<'AREOF' 2>/dev/null || true
import json, os, time, uuid, hashlib, sys
from pathlib import Path

HOME = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit")))
try:
    ev = json.loads(os.environ.get("EVENT_JSON", "") or "{}")
except Exception:
    ev = {}
if not isinstance(ev, dict):
    raise SystemExit(0)

tool_name = str(ev.get("tool_name", "") or "")
# Only record the subagent-spawn tool; no-op for anything else.
if tool_name not in ("Task", "Agent"):
    raise SystemExit(0)

ti = ev.get("tool_input") or {}
if not isinstance(ti, dict):
    ti = {}
tr = ev.get("tool_response")
cwd = str(ev.get("cwd", "") or os.getcwd())
session_id = str(ev.get("session_id", "") or "")

subagent_type = (str(ti.get("subagent_type", "") or "").strip()) or "engineering"
description = str(ti.get("description", "") or "").strip()
prompt = str(ti.get("prompt", "") or "").strip()
title = (description or prompt[:80] or ("subagent " + subagent_type))[:120]

# Outcome + result text from the tool_response shape (best-effort).
outcome = "success"
result_text = ""
try:
    if isinstance(tr, dict):
        if tr.get("is_error") or tr.get("error"):
            outcome = "error"
        result_text = str(tr.get("result") or tr.get("content") or tr.get("output") or "")
    elif isinstance(tr, list):
        parts = []
        for b in tr:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(str(b["text"]))
        result_text = "\\n".join(parts)
    elif isinstance(tr, str):
        result_text = tr
except Exception:
    pass
result_text = result_text[:2000]

venture = os.path.basename(cwd.rstrip("/")) or "all"
ext_key = "harness:%s:%s" % (
    session_id[:12],
    hashlib.sha1((prompt or title).encode("utf-8", "replace")).hexdigest()[:10],
)
variables = {
    "model": subagent_type,      # harness does not expose the subagent's model;
                                 # subagent_type is the best available identity proxy
    "outcome": outcome,
    "source": "harness-hook",
    "tool": tool_name,
}

# --- Preferred path: the bundled backend (schema-authoritative) ---
recorded = False
try:
    sys.path.insert(0, str(HOME / "server"))
    from ai import agent_dispatch as ad
    disp = ad.dispatch_task(
        title=title,
        description=(description or prompt[:500]),
        assignee="any",              # backend validates assignee to a known model
                                     # set; the harness does not expose the
                                     # subagent's model, so identity lives in
                                     # task_type + variables.model
        priority="P2",
        task_type=subagent_type,
        venture=venture,
        context=prompt[:1000],
        variables=variables,
        external_key=ext_key,
    )
    tid = disp.get("task_id") if isinstance(disp, dict) else None
    if tid:
        ad.complete_task(
            tid,
            result=(result_text or "(subagent completed; no captured result)"),
            files_changed=[],
        )
        recorded = True
except Exception:
    recorded = False

# --- Fallback: direct additive write to tasks.json (same schema) ---
if not recorded:
    try:
        adir = HOME / "agents"
        adir.mkdir(parents=True, exist_ok=True)
        tf = adir / "tasks.json"
        tasks = {}
        if tf.exists():
            try:
                tasks = json.loads(tf.read_text())
            except Exception:
                tasks = {}
        if not isinstance(tasks, dict):
            tasks = {}
        if not any(isinstance(t, dict) and t.get("external_key") == ext_key for t in tasks.values()):
            tid = "AGT-" + uuid.uuid4().hex[:8].upper()
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            tasks[tid] = {
                "id": tid, "title": title,
                "description": (description or prompt[:500]),
                "assignee": "any", "priority": "P2",
                "tools_needed": [], "constraints": [], "context": prompt[:1000],
                "task_type": subagent_type, "venture": venture,
                "variables": variables, "external_key": ext_key,
                "status": "done", "created_at": now, "updated_at": now,
                "completed_at": now, "files_changed": [],
                "result": (result_text or "(subagent completed)"), "handoffs": [],
            }
            tf.write_text(json.dumps(tasks, indent=2))
    except Exception:
        pass
AREOF
exit 0
`;
        fs.writeFileSync(recorderScript, recorderContent);
        fs.chmodSync(recorderScript, '755');

        if (!config.hooks.PostToolUse) {
            config.hooks.PostToolUse = [];
        }
        const existingRecorder = config.hooks.PostToolUse.find(group => {
            const cmds = (group.hooks || []).map(h => h.command || '');
            return cmds.some(c => c.includes('delimit-agent-record'));
        });
        if (!existingRecorder) {
            config.hooks.PostToolUse.push({
                matcher: 'Task|Agent',
                hooks: [{
                    type: 'command',
                    command: recorderScript,
                    timeout: 10,
                }],
            });
            changes.push('PostToolUse:agent-record');
        }
    }

    // Write hooks to all target settings files
    const configJson = JSON.stringify(config, null, 2);
    for (const target of writeTargets) {
        try {
            if (target === configPath) {
                // Global ~/.claude/settings.json: write the merged config we built
                fs.writeFileSync(target, configJson);
                continue;
            }

            // Project settings (.claude/settings.json in cwd): merge ONLY the
            // Delimit-added hook entries into existing project hooks. Never
            // overwrite the project's own hook entries with global ones.
            // Previous behavior (`existing.hooks = config.hooks`) propagated
            // every global hook into project files, wiping project-local hooks
            // and leaking unrelated user customizations across repos.
            let existing = {};
            if (fs.existsSync(target)) {
                try { existing = JSON.parse(fs.readFileSync(target, 'utf-8')); } catch { existing = {}; }
            }
            if (!existing.hooks) existing.hooks = {};

            for (const [event, groups] of Object.entries(config.hooks || {})) {
                if (!Array.isArray(groups)) continue;
                if (!existing.hooks[event]) existing.hooks[event] = [];
                for (const group of groups) {
                    const cmds = (group.hooks || []).map(h => h.command || '');
                    // Only propagate Delimit-owned hook groups to project files
                    if (!cmds.some(c => c.includes('delimit'))) continue;
                    const alreadyHas = existing.hooks[event].some(eg =>
                        (eg.hooks || []).some(h => cmds.includes(h.command))
                    );
                    if (!alreadyHas) existing.hooks[event].push(group);
                }
            }
            fs.writeFileSync(target, JSON.stringify(existing, null, 2));
        } catch {}
    }
    return changes;
}

/**
 * Install hooks for Codex CLI.
 * Codex uses instructions.md for session-start equivalent and config.json for settings.
 * We add governance instructions and a pre-commit hook reference.
 */
function installCodexHooks(tool, hookConfig) {
    const changes = [];
    const codexDir = path.dirname(tool.configPath);
    fs.mkdirSync(codexDir, { recursive: true });

    // Codex instructions.md -- acts as the session-start equivalent
    if (hookConfig.session_start) {
        const instructionsPath = tool.instructionsPath || path.join(codexDir, 'instructions.md');
        // LED-213: Use canonical Consensus 123 template for Codex parity
        const delimitBlock = `<!-- delimit:hooks-start -->
${getDelimitSection()}
<!-- delimit:hooks-end -->`;

        let content = '';
        if (fs.existsSync(instructionsPath)) {
            content = fs.readFileSync(instructionsPath, 'utf-8');
        }

        if (content.includes('delimit:hooks-start')) {
            // Replace existing block
            content = content.replace(
                /<!-- delimit:hooks-start -->[\s\S]*?<!-- delimit:hooks-end -->/,
                delimitBlock
            );
        } else {
            content = content ? content + '\n\n' + delimitBlock : delimitBlock;
        }

        fs.writeFileSync(instructionsPath, content);
        changes.push('instructions.md');
    }

    // Codex config.json -- add hook commands
    let config = {};
    if (fs.existsSync(tool.configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        } catch { config = {}; }
    }

    if (!config.hooks) {
        config.hooks = {};
    }

    if (hookConfig.pre_commit && !config.hooks['pre-commit']) {
        config.hooks['pre-commit'] = 'npx delimit-cli hook pre-commit';
        changes.push('pre-commit hook');
    }

    fs.writeFileSync(tool.configPath, JSON.stringify(config, null, 2));
    return changes;
}

/**
 * Install hooks for Antigravity CLI.
 */
function installAntigravityHooks(tool, hookConfig) {
    const changes = [];
    const antigravityDir = path.dirname(tool.configPath);
    fs.mkdirSync(antigravityDir, { recursive: true });

    // Update settings.json with custom instructions
    let config = {};
    if (fs.existsSync(tool.configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        } catch { config = {}; }
    }

    const govInstructions = getDelimitSectionCondensed();
    const DELIMIT_MARKER = '<!-- delimit:start';

    if (!config.customInstructions || !config.customInstructions.includes(DELIMIT_MARKER)) {
        config.customInstructions = govInstructions;
        changes.push('customInstructions');
    }

    fs.writeFileSync(tool.configPath, JSON.stringify(config, null, 2));

    // ANTIGRAVITY.md: use the same upsert pattern as CLAUDE.md
    const antigravityMd = path.join(getHome(), 'ANTIGRAVITY.md');
    const managedSection = getDelimitSection();
    if (!fs.existsSync(antigravityMd)) {
        fs.writeFileSync(antigravityMd, managedSection + '\n');
        changes.push('ANTIGRAVITY.md');
    } else {
        const existing = fs.readFileSync(antigravityMd, 'utf-8');
        if (existing.includes(DELIMIT_MARKER) && existing.includes('<!-- delimit:end -->')) {
            const before = existing.substring(0, existing.indexOf(DELIMIT_MARKER));
            const after = existing.substring(existing.indexOf('<!-- delimit:end -->') + '<!-- delimit:end -->'.length);
            const updated = before + managedSection + after;
            if (updated !== existing) {
                fs.writeFileSync(antigravityMd, updated);
                changes.push('ANTIGRAVITY.md');
            }
        } else {
            const sep = existing.endsWith('\n') ? '\n' : '\n\n';
            fs.writeFileSync(antigravityMd, existing + sep + managedSection + '\n');
            changes.push('ANTIGRAVITY.md');
        }
    }

    return changes;
}

/**
 * Install hooks for Gemini CLI.
 * Gemini CLI uses MCP (already handled by setup) but we add governance
 * instructions to settings.json and a GEMINI.md equivalent.
 */
function installGeminiHooks(tool, hookConfig) {
    const changes = [];
    const geminiDir = path.dirname(tool.configPath);
    fs.mkdirSync(geminiDir, { recursive: true });

    // Update settings.json with custom instructions
    let config = {};
    if (fs.existsSync(tool.configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        } catch { config = {}; }
    }

    // LED-213: canonical governance template (condensed for JSON).
    // Detect via the stable <!-- delimit:start --> marker, not a prose phrase
    // that may change between versions.
    const govInstructions = getDelimitSectionCondensed();
    const DELIMIT_MARKER = '<!-- delimit:start';

    if (!config.customInstructions || !config.customInstructions.includes(DELIMIT_MARKER)) {
        config.customInstructions = govInstructions;
        changes.push('customInstructions');
    }

    fs.writeFileSync(tool.configPath, JSON.stringify(config, null, 2));

    // GEMINI.md: use the same upsert pattern as CLAUDE.md so user content
    // outside the managed markers is preserved across delimit-cli upgrades.
    const geminiMd = path.join(geminiDir, 'GEMINI.md');
    const managedSection = getDelimitSection();
    if (!fs.existsSync(geminiMd)) {
        fs.writeFileSync(geminiMd, managedSection + '\n');
        changes.push('GEMINI.md');
    } else {
        const existing = fs.readFileSync(geminiMd, 'utf-8');
        if (existing.includes(DELIMIT_MARKER) && existing.includes('<!-- delimit:end -->')) {
            // Replace only the managed region
            const before = existing.substring(0, existing.indexOf(DELIMIT_MARKER));
            const after = existing.substring(existing.indexOf('<!-- delimit:end -->') + '<!-- delimit:end -->'.length);
            const updated = before + managedSection + after;
            if (updated !== existing) {
                fs.writeFileSync(geminiMd, updated);
                changes.push('GEMINI.md');
            }
        } else {
            // Append managed section below existing user content
            const sep = existing.endsWith('\n') ? '\n' : '\n\n';
            fs.writeFileSync(geminiMd, existing + sep + managedSection + '\n');
            changes.push('GEMINI.md');
        }
    }

    return changes;
}

/**
 * Install hooks for a detected tool.
 * Returns { tool, changes } describing what was installed.
 */
function installHooksForTool(tool, hookConfig) {
    switch (tool.id) {
        case 'claude':
            return { tool, changes: installClaudeHooks(tool, hookConfig) };
        case 'codex':
            return { tool, changes: installCodexHooks(tool, hookConfig) };
        case 'gemini':
            return { tool, changes: installGeminiHooks(tool, hookConfig) };
        case 'antigravity':
            return { tool, changes: installAntigravityHooks(tool, hookConfig) };
        default:
            return { tool, changes: [] };
    }
}

/**
 * Install hooks for all detected AI tools.
 */
function installAllHooks(hookConfig) {
    const tools = detectAITools();
    const results = [];
    for (const tool of tools) {
        results.push(installHooksForTool(tool, hookConfig));
    }
    return { tools, results };
}

// ---------------------------------------------------------------------------
// Hook removal (for uninstall)
// ---------------------------------------------------------------------------

function removeClaudeHooks() {
    const configPath = path.join(getHome(), '.claude', 'settings.json');
    if (!fs.existsSync(configPath)) return false;

    try {
        const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
        if (!config.hooks) return false;

        let changed = false;

        for (const event of ['SessionStart', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd']) {
            if (Array.isArray(config.hooks[event])) {
                const before = config.hooks[event].length;
                config.hooks[event] = config.hooks[event].filter(h => {
                    const isDelimit = (cmd) => cmd && (cmd.includes('delimit-cli') || cmd.includes('delimit'));
                    // Nested format: check hooks[].command
                    if (h.hooks && Array.isArray(h.hooks)) {
                        return !h.hooks.some(inner => isDelimit(inner.command));
                    }
                    // Flat format: check h.command directly
                    return !isDelimit(h.command);
                });
                if (config.hooks[event].length === 0) {
                    delete config.hooks[event];
                }
                if (config.hooks[event] === undefined || config.hooks[event].length < before) {
                    changed = true;
                }
            }
        }

        if (Object.keys(config.hooks).length === 0) {
            delete config.hooks;
        }

        if (changed) {
            fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
        }
        return changed;
    } catch {
        return false;
    }
}

function removeCodexHooks() {
    let changed = false;

    // Remove from instructions.md
    const instructionsPath = path.join(getHome(), '.codex', 'instructions.md');
    if (fs.existsSync(instructionsPath)) {
        let content = fs.readFileSync(instructionsPath, 'utf-8');
        if (content.includes('delimit:hooks-start')) {
            content = content.replace(
                /\n*<!-- delimit:hooks-start -->[\s\S]*?<!-- delimit:hooks-end -->\n*/,
                ''
            );
            fs.writeFileSync(instructionsPath, content);
            changed = true;
        }
    }

    // Remove hooks from config.json
    const configPath = path.join(getHome(), '.codex', 'config.json');
    if (fs.existsSync(configPath)) {
        try {
            const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
            if (config.hooks) {
                for (const [key, val] of Object.entries(config.hooks)) {
                    if (typeof val === 'string' && val.includes('delimit-cli')) {
                        delete config.hooks[key];
                        changed = true;
                    }
                }
                if (Object.keys(config.hooks).length === 0) {
                    delete config.hooks;
                }
                if (changed) {
                    fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
                }
            }
        } catch { /* ignore */ }
    }

    return changed;
}

function removeGeminiHooks() {
    let changed = false;

    // Remove custom instructions referencing delimit
    const configPath = path.join(getHome(), '.gemini', 'settings.json');
    if (fs.existsSync(configPath)) {
        try {
            const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
            if (config.customInstructions && config.customInstructions.includes('delimit-cli hook')) {
                delete config.customInstructions;
                fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
                changed = true;
            }
        } catch { /* ignore */ }
    }

    // Remove GEMINI.md if it's ours
    const geminiMd = path.join(getHome(), '.gemini', 'GEMINI.md');
    if (fs.existsSync(geminiMd)) {
        const content = fs.readFileSync(geminiMd, 'utf-8');
        if (content.includes('Delimit Governance')) {
            fs.unlinkSync(geminiMd);
            changed = true;
        }
    }

    return changed;
}

function removeAntigravityHooks() {
    let changed = false;

    // Remove custom instructions referencing delimit
    const configPath = path.join(getHome(), '.gemini', 'antigravity-cli', 'settings.json');
    if (fs.existsSync(configPath)) {
        try {
            const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
            if (config.customInstructions && config.customInstructions.includes('delimit-cli hook')) {
                delete config.customInstructions;
                fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
                changed = true;
            }
        } catch { /* ignore */ }
    }

    // Remove ANTIGRAVITY.md if it's ours
    const antigravityMd = path.join(getHome(), 'ANTIGRAVITY.md');
    if (fs.existsSync(antigravityMd)) {
        const content = fs.readFileSync(antigravityMd, 'utf-8');
        if (content.includes('Delimit Governance')) {
            fs.unlinkSync(antigravityMd);
            changed = true;
        }
    }

    return changed;
}

function removeAllHooks() {
    const results = [];

    if (removeClaudeHooks()) {
        results.push('Claude Code');
    }
    if (removeCodexHooks()) {
        results.push('Codex CLI');
    }
    if (removeGeminiHooks()) {
        results.push('Gemini CLI');
    }
    if (removeAntigravityHooks()) {
        results.push('Antigravity CLI');
    }

    return results;
}

// ---------------------------------------------------------------------------
// Deliberation helpers
// ---------------------------------------------------------------------------

/**
 * Count pending strategy items in the ledger that have priority P0.
 * Returns the count of open/in_progress P0 strategy items.
 */
function countPendingStrategyItems() {
    const ledgerDir = path.join(getDelimitHome(), 'ledger');
    if (!fs.existsSync(ledgerDir)) return 0;

    let count = 0;
    try {
        const files = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
        for (const f of files) {
            try {
                const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, f), 'utf-8'));
                if (!Array.isArray(items)) continue;
                for (const item of items) {
                    const isOpen = item.status === 'open' || item.status === 'in_progress';
                    const isStrategy = item.category === 'strategy' || item.category === 'deliberation';
                    const isP0 = item.priority === 'P0' || item.priority === 0;
                    if (isOpen && (isStrategy || isP0)) {
                        count++;
                    }
                }
            } catch { /* ignore individual file parse errors */ }
        }
    } catch { /* ignore directory read errors */ }

    return count;
}

/**
 * Get the highest priority pending strategy item from the ledger.
 * Returns the item object or null if none found.
 */
function getTopStrategyItem() {
    const ledgerDir = path.join(getDelimitHome(), 'ledger');
    if (!fs.existsSync(ledgerDir)) return null;

    let best = null;
    const priorityOrder = { P0: 0, P1: 1, P2: 2, P3: 3 };

    try {
        const files = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
        for (const f of files) {
            try {
                const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, f), 'utf-8'));
                if (!Array.isArray(items)) continue;
                for (const item of items) {
                    const isOpen = item.status === 'open' || item.status === 'in_progress';
                    const isStrategy = item.category === 'strategy' || item.category === 'deliberation';
                    const isP0 = item.priority === 'P0' || item.priority === 0;
                    if (isOpen && (isStrategy || isP0)) {
                        const rank = typeof item.priority === 'number' ? item.priority : (priorityOrder[item.priority] ?? 99);
                        if (!best || rank < (typeof best.priority === 'number' ? best.priority : (priorityOrder[best.priority] ?? 99))) {
                            best = item;
                        }
                    }
                }
            } catch { /* ignore */ }
        }
    } catch { /* ignore */ }

    return best;
}

// ---------------------------------------------------------------------------
// Hook execution commands
// ---------------------------------------------------------------------------

/**
 * session-start: Show ledger context and governance health.
 * Output goes to stdout for the AI tool to read.
 */
async function hookSessionStart() {
    const config = loadHookConfig();
    // Always show status — even if session_start is false in config
    // This is the first thing a user sees. Make it count.

    const lines = [];
    lines.push('=== Delimit Status ===');

    const home = getHome();
    const delimitHome = path.join(home, '.delimit');
    const cwd = process.cwd();

    // Governance status + policy source
    const projectPolicy = fs.existsSync(path.join(cwd, 'delimit.yml')) || fs.existsSync(path.join(cwd, '.delimit', 'policies.yml'));
    const userPolicy = fs.existsSync(path.join(delimitHome, 'delimit.yml'));
    if (projectPolicy) {
        lines.push('Governance: active | policy=project');
    } else if (userPolicy) {
        lines.push('Governance: active | policy=user');
    } else {
        lines.push('Governance: not initialized -- run npx delimit-cli init');
    }

    // Server status + tool count
    const serverFile = path.join(delimitHome, 'server', 'ai', 'server.py');
    if (fs.existsSync(serverFile)) {
        try {
            const content = fs.readFileSync(serverFile, 'utf-8');
            const toolCount = (content.match(/@mcp\.tool\(\)/g) || []).length;
            lines.push(`Server: ready (${toolCount} tools)`);
        } catch {
            lines.push('Server: ready');
        }
    } else {
        lines.push('Server: not installed -- run npx delimit-cli setup');
    }

    // Hooks + audit
    const settingsFile = path.join(home, '.claude', 'settings.json');
    const hooksEnabled = fs.existsSync(settingsFile) && fs.readFileSync(settingsFile, 'utf-8').includes('"hooks"');
    const auditOn = fs.existsSync(path.join(delimitHome, 'audit'));
    lines.push(`Hooks: ${hooksEnabled ? 'enabled' : 'disabled'} | Audit: ${auditOn ? 'on' : 'off'}`);

    // MCP registration
    const mcpFile = path.join(home, '.mcp.json');
    const mcpRegistered = fs.existsSync(mcpFile) && fs.readFileSync(mcpFile, 'utf-8').includes('delimit');
    lines.push(`MCP: ${mcpRegistered ? 'delimit registered' : 'not registered -- run npx delimit-cli setup'}`);

    // Deliberation models
    const modelsFile = path.join(delimitHome, 'models.json');
    const modelNames = [];
    try {
        if (fs.existsSync(modelsFile)) {
            const models = JSON.parse(fs.readFileSync(modelsFile, 'utf-8'));
            for (const [key, val] of Object.entries(models)) {
                if (val && val.enabled) modelNames.push(val.name || key);
            }
        }
    } catch {}
    // Also check env vars for available models
    if (modelNames.length === 0) {
        const envModels = [];
        if (process.env.XAI_API_KEY) envModels.push('Grok');
        if (process.env.GOOGLE_APPLICATION_CREDENTIALS) envModels.push('Gemini');
        if (process.env.OPENAI_API_KEY) envModels.push('Codex');
        if (envModels.length > 0) {
            lines.push(`Deliberation: ${envModels.join(' + ')}`);
        }
    } else {
        lines.push(`Deliberation: ${modelNames.join(' + ')}`);
    }

    // Recent sessions (prevents cross-session drift). Subagent sessions get
    // a scoped one-liner instead of the multi-session digest, so a narrow
    // dispatched task does not pull in the orchestrator's global context.
    const sessionType = process.env.DELIMIT_SESSION_TYPE;
    if (sessionType === 'subagent' || sessionType === 'agent') {
        lines.push(`Session: subagent (scoped) | handoff=${process.env.DELIMIT_HANDOFF_ID || 'none'}`);
        lines.push(`Revive scoped context with: delimit_revive(scope="${process.env.DELIMIT_HANDOFF_ID || '<handoff_id>'}")`);
    } else {
        const sessionsDir = path.join(delimitHome, 'sessions');
        try {
            if (fs.existsSync(sessionsDir)) {
                const sessions = fs.readdirSync(sessionsDir).filter(f => f.startsWith('session_')).sort().reverse();
                if (sessions.length > 0) {
                    lines.push('Recent sessions (revive full state via delimit_revive + delimit_session_history):');
                    for (const s of sessions.slice(0, 8)) {
                        try {
                            const d = JSON.parse(fs.readFileSync(path.join(sessionsDir, s), 'utf-8'));
                            const ts = (d.timestamp || '?').substring(0, 10);
                            const summary = (d.summary || '(no summary)').substring(0, 160);
                            lines.push(`  - ${ts} ${summary}`);
                        } catch {}
                    }
                }
            }
        } catch {}
    }

    // Auto-update check + install
    try {
        const pkgPath = path.join(__dirname, '..', 'package.json');
        const currentVersion = JSON.parse(fs.readFileSync(pkgPath, 'utf-8')).version;
        const { execSync: execS } = require('child_process');
        const latest = execS('npm view delimit-cli version 2>/dev/null', { encoding: 'utf-8', timeout: 5000 }).trim();
        if (latest && latest !== currentVersion && latest > currentVersion) {
            lines.push(`[Delimit] Updating ${currentVersion} -> ${latest}...`);
            try {
                execS('npm install -g delimit-cli@latest 2>/dev/null', { timeout: 30000, stdio: 'pipe' });
                execS('delimit-cli setup 2>/dev/null', { timeout: 30000, stdio: 'pipe' });
                lines.push(`[Delimit] Updated to ${latest}`);
            } catch {
                lines.push(`[Delimit] Auto-update failed. Run: npm install -g delimit-cli@latest`);
            }
        }
    } catch { /* offline or timeout — skip silently */ }

    // Check for OpenAPI specs
    const specPatterns = ['openapi.yaml', 'openapi.yml', 'openapi.json', 'swagger.yaml', 'swagger.json'];
    const foundSpecs = [];
    for (const pattern of specPatterns) {
        const specPath = path.join(cwd, pattern);
        if (fs.existsSync(specPath)) {
            foundSpecs.push(pattern);
        }
    }
    // Also check api/ and specs/ directories
    for (const dir of ['api', 'specs', 'spec']) {
        const dirPath = path.join(cwd, dir);
        if (fs.existsSync(dirPath)) {
            try {
                const files = fs.readdirSync(dirPath);
                for (const f of files) {
                    if (/\.(yaml|yml|json)$/.test(f) && /openapi|swagger/i.test(f)) {
                        foundSpecs.push(path.join(dir, f));
                    }
                }
            } catch { /* ignore */ }
        }
    }

    if (foundSpecs.length > 0) {
        lines.push(`[Delimit] OpenAPI specs detected: ${foundSpecs.join(', ')}`);
    }

    // Check ledger
    const ledgerDir = path.join(getDelimitHome(), 'ledger');
    if (fs.existsSync(ledgerDir)) {
        try {
            const ledgerFiles = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
            let openItems = 0;
            for (const f of ledgerFiles) {
                try {
                    const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, f), 'utf-8'));
                    if (Array.isArray(items)) {
                        openItems += items.filter(i => i.status === 'open' || i.status === 'in_progress').length;
                    }
                } catch { /* ignore */ }
            }
            if (openItems > 0) {
                lines.push(`[Delimit] Ledger: ${openItems} open item(s)`);
            } else {
                lines.push('[Delimit] Ledger: no open items');
            }
        } catch {
            lines.push('[Delimit] Ledger: empty');
        }
    }

    // Check for pending strategy items that need deliberation
    if (config.show_strategy_items) {
        const strategyCount = countPendingStrategyItems();
        if (strategyCount > 0) {
            lines.push(`[delimit] ${strategyCount} strategic decision${strategyCount === 1 ? '' : 's'} pending deliberation. Run: delimit deliberate`);
        }
    }

    // Git branch info
    try {
        const branch = execSync('git branch --show-current 2>/dev/null', { encoding: 'utf-8' }).trim();
        if (branch) {
            lines.push(`[Delimit] Branch: ${branch}`);
        }
    } catch { /* not in git repo */ }

    lines.push('=== Delimit Ready ===');
    lines.push('');
    process.stdout.write(lines.join('\n') + '\n');
}

/**
 * bootstrap: shared natural-language trigger handler.
 * execute -> resume or launch governed work loop
 * inspect -> show ledger/daemon/continuity state without executing
 */
async function hookBootstrap(mode = 'inspect', options = {}) {
    const cwd = options.cwd || process.cwd();
    const lines = [];
    const silent = Boolean(options.silent);
    const normalizedMode = mode === 'execute' ? 'execute' : 'inspect';
    const { resolveContinuityContext } = require('./continuity-resolver');
    const context = resolveContinuityContext({ cwd, scope: options.scope });
    const hasPolicy = fs.existsSync(path.join(cwd, 'delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit', 'policies.yml'));
    const globalLedgerDir = context.ledgerRoot;
    const sessionDir = path.join(getDelimitHome(), 'sessions');
    const ledgerState = buildLedgerState(globalLedgerDir);
    const latestSession = readLatestSessionSummary(sessionDir);
    const inboxDaemon = getServiceState('delimit-inbox.service');
    const socialDaemon = getServiceState('delimit-social-scan.service');

    lines.push('[Delimit] Bootstrap');
    lines.push(`[Delimit] Mode: ${normalizedMode}`);
    lines.push(`[Delimit] Repo: ${cwd}`);
    lines.push(`[Delimit] Actor: ${context.actor}`);
    lines.push(`[Delimit] Venture: ${context.venture}`);
    lines.push(`[Delimit] Continuity root: ${context.continuityRoot}`);
    lines.push(`[Delimit] Ledger scope: ${context.ledgerScope}`);
    lines.push(hasPolicy ? '[Delimit] Governance: active' : '[Delimit] Governance: repo policy missing');
    lines.push(fs.existsSync(globalLedgerDir) ? '[Delimit] Ledger: available' : '[Delimit] Ledger: unavailable');
    lines.push(fs.existsSync(sessionDir) ? '[Delimit] Continuity: session history available' : '[Delimit] Continuity: no saved sessions');
    lines.push(`[Delimit] Inbox daemon: ${inboxDaemon.active}/${inboxDaemon.enabled}`);
    lines.push(`[Delimit] Social daemon: ${socialDaemon.active}/${socialDaemon.enabled}`);

    if (latestSession) {
        lines.push(`[Delimit] Latest session: ${latestSession.id}`);
        if (latestSession.summary) {
            lines.push(`[Delimit] Latest summary: ${latestSession.summary}`);
        }
        if (latestSession.blockers.length > 0) {
            lines.push(`[Delimit] Blockers: ${latestSession.blockers.join('; ')}`);
        }
    }

    if (ledgerState.next) {
        const next = ledgerState.next;
        lines.push(`[Delimit] Next open item: ${next.id} ${next.title || '(untitled)'} [${next.priority || 'P?'}]`);
    } else {
        lines.push('[Delimit] Next open item: none');
    }

    if (normalizedMode === 'execute') {
        const bootstrapState = {
            timestamp: new Date().toISOString(),
            actor: context.actor,
            venture: context.venture,
            repo: cwd,
            mode: normalizedMode,
            nextItem: ledgerState.next ? {
                id: ledgerState.next.id,
                title: ledgerState.next.title || '',
                priority: ledgerState.next.priority || '',
                status: ledgerState.next.status || 'open',
            } : null,
            daemons: {
                inbox: inboxDaemon,
                social: socialDaemon,
            },
            latestSession,
            openItemCount: ledgerState.open.length,
        };
        const statePath = writeBootstrapState(context.continuityRoot, bootstrapState);
        lines.push('[Delimit] Intent: resume or launch governed persistent loop');
        lines.push(`[Delimit] Work order saved: ${statePath}`);
        lines.push('[Delimit] Next tools: delimit session --build');
    } else {
        lines.push('[Delimit] Intent: inspect current state without executing');
        lines.push('[Delimit] Next tools: delimit session --inspect');
    }

    const payload = {
        mode: normalizedMode,
        repo: cwd,
        actor: context.actor,
        venture: context.venture,
        continuityRoot: context.continuityRoot,
        ledgerRoot: context.ledgerRoot,
        ledgerScope: context.ledgerScope,
        hasPolicy,
        ledgerAvailable: fs.existsSync(globalLedgerDir),
        continuityAvailable: fs.existsSync(sessionDir),
        daemons: {
            inbox: inboxDaemon,
            social: socialDaemon,
        },
        latestSession,
        nextItem: ledgerState.next || null,
        openItemCount: ledgerState.open.length,
    };
    if (!silent) {
        lines.push('');
        process.stdout.write(lines.join('\n') + '\n');
    }
    return payload;
}

/**
 * pre-tool: Check before file edits.
 * If editing an OpenAPI spec, run a quick lint.
 * If editing a test file, note it.
 */
async function hookPreTool(toolName) {
    const config = loadHookConfig();
    if (!config.pre_tool) {
        return;
    }

    // The tool name comes from the AI tool (e.g., "Edit", "Write", "Bash")
    // We check the DELIMIT_TOOL_INPUT env or just do lightweight checks
    const cwd = process.cwd();

    // Check if there are staged OpenAPI spec changes
    try {
        const stagedFiles = execSync('git diff --cached --name-only 2>/dev/null', {
            encoding: 'utf-8',
            timeout: 2000,
        }).split('\n').filter(Boolean);

        const specFiles = stagedFiles.filter(f =>
            /openapi|swagger/i.test(f) && /\.(yaml|yml|json)$/.test(f)
        );

        if (specFiles.length > 0) {
            process.stderr.write(`[Delimit] Warning: OpenAPI spec(s) staged for commit: ${specFiles.join(', ')}\n`);
            process.stderr.write('[Delimit] Run "delimit lint" before committing to check for breaking changes.\n');
        }

        const testFiles = stagedFiles.filter(f =>
            /\.(test|spec)\.(js|ts|py|rb)$/.test(f) || /test_.*\.py$/.test(f)
        );

        if (testFiles.length > 0) {
            process.stderr.write(`[Delimit] Test files staged: ${testFiles.join(', ')}\n`);
            process.stderr.write('[Delimit] Consider running tests before committing.\n');
        }
    } catch {
        // Not in a git repo or no staged changes -- that is fine
    }
}

/**
 * pre-commit: Run repo diagnostics before committing.
 */
async function hookPreCommit() {
    const config = loadHookConfig();
    if (!config.pre_commit) {
        return;
    }

    const cwd = process.cwd();
    const warnings = [];

    // Check for staged OpenAPI spec changes
    try {
        const stagedFiles = execSync('git diff --cached --name-only 2>/dev/null', {
            encoding: 'utf-8',
            timeout: 2000,
        }).split('\n').filter(Boolean);

        const specFiles = stagedFiles.filter(f =>
            /openapi|swagger/i.test(f) && /\.(yaml|yml|json)$/.test(f)
        );

        if (specFiles.length > 0) {
            // Try to find a previous version to diff against
            for (const specFile of specFiles) {
                try {
                    // Get the HEAD version
                    const oldContent = execSync(`git show HEAD:${specFile} 2>/dev/null`, {
                        encoding: 'utf-8',
                        timeout: 3000,
                    });
                    if (oldContent) {
                        warnings.push(`[Delimit] OpenAPI spec changed: ${specFile}`);
                        warnings.push('[Delimit] Run "delimit diff <old> <new>" to review API changes before committing.');
                    }
                } catch {
                    // New file, no previous version
                }
            }
        }

        // Check for secrets patterns in staged files
        const sensitivePatterns = [
            /password\s*[:=]\s*['"][^'"]+['"]/i,
            /api[_-]?key\s*[:=]\s*['"][^'"]+['"]/i,
            /secret\s*[:=]\s*['"][^'"]+['"]/i,
        ];

        for (const file of stagedFiles) {
            if (/\.(env|key|pem|p12|pfx)$/.test(file)) {
                warnings.push(`[Delimit] WARNING: Potentially sensitive file staged: ${file}`);
            }
        }
    } catch {
        // Not in git repo
    }

    // Check for policy file
    const hasPolicy = fs.existsSync(path.join(cwd, 'delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit.yml'));

    if (!hasPolicy) {
        warnings.push('[Delimit] No governance policy found. Run "delimit init" to create one.');
    }

    // Deliberation on API spec commits (opt-in via deliberate_on_commit)
    if (config.deliberate_on_commit) {
        try {
            const stagedFiles2 = execSync('git diff --cached --name-only 2>/dev/null', {
                encoding: 'utf-8',
                timeout: 2000,
            }).split('\n').filter(Boolean);

            const apiSpecFiles = stagedFiles2.filter(f =>
                /openapi|swagger/i.test(f) && /\.(yaml|yml|json)$/.test(f)
            );

            if (apiSpecFiles.length > 0) {
                // Auto-deliberate: call Delimit gateway directly
                if (config.deliberate_on_commit === 'auto') {
                    process.stderr.write('[delimit] API spec change detected — running multi-model deliberation...\n');
                    try {
                        const diff = execSync(`git diff --cached -- ${apiSpecFiles.join(' ')} 2>/dev/null`, {
                            encoding: 'utf-8',
                            timeout: 5000,
                            maxBuffer: 50 * 1024,
                        }).slice(0, 2000);
                        const question = `This commit modifies API specs (${apiSpecFiles.join(', ')}). Is this change safe to ship? Are there breaking changes?\n\nDiff:\n${diff}`;
                        const result = execSync(`npx delimit-cli deliberate --question "${question.replace(/"/g, '\\"')}" --mode quick 2>/dev/null`, {
                            encoding: 'utf-8',
                            timeout: 60000,
                        });
                        process.stderr.write(result + '\n');
                    } catch (e) {
                        warnings.push(`[delimit] Deliberation failed: ${e.message?.slice(0, 100) || 'timeout'}. Proceeding with commit.`);
                    }
                } else {
                    warnings.push('[delimit] This commit modifies API specs. Consider running: delimit deliberate "Is this change safe?"');
                }
            }
        } catch { /* not in git repo */ }
    }

    if (warnings.length > 0) {
        process.stderr.write(warnings.join('\n') + '\n');
    }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------
// Deploy gate hook — runs smoke test before any deploy (LED-024 feedback)
// ---------------------------------------------------------------------------

async function hookDeployGate() {
    const lines = [];
    lines.push('[Delimit] Deploy gate check');
    lines.push('');

    let blocked = false;

    // 1. Check for common import/syntax errors
    const cwd = process.cwd();
    const hasDockerCompose = fs.existsSync(path.join(cwd, 'docker-compose.yml'))
        || fs.existsSync(path.join(cwd, 'docker-compose.yaml'))
        || fs.existsSync(path.join(cwd, 'compose.yml'));

    // 2. Check for Python import errors if it's a Python project
    const hasPython = fs.existsSync(path.join(cwd, 'requirements.txt'))
        || fs.existsSync(path.join(cwd, 'pyproject.toml'))
        || fs.existsSync(path.join(cwd, 'setup.py'));

    if (hasPython) {
        try {
            // Find the main app module
            const appDirs = ['app', 'src', 'api'];
            for (const dir of appDirs) {
                const initFile = path.join(cwd, dir, '__init__.py');
                const mainFile = path.join(cwd, dir, 'main.py');
                if (fs.existsSync(initFile) || fs.existsSync(mainFile)) {
                    try {
                        execSync(`python3 -c "import ${dir}" 2>&1`, {
                            encoding: 'utf-8',
                            timeout: 10000,
                            cwd,
                        });
                        lines.push(`[Delimit] ✓ ${dir}/ imports clean`);
                    } catch (e) {
                        lines.push(`[Delimit] ✗ ${dir}/ import error: ${e.stdout || e.stderr || e.message}`);
                        blocked = true;
                    }
                }
            }
        } catch { /* ignore */ }
    }

    // 3. Check for Node.js syntax errors
    const hasNode = fs.existsSync(path.join(cwd, 'package.json'));
    if (hasNode) {
        try {
            execSync('node -e "require(\'./\')" 2>&1', {
                encoding: 'utf-8',
                timeout: 5000,
                cwd,
            });
            lines.push('[Delimit] ✓ Node.js entry point loads');
        } catch {
            // Not all projects have a main entry — skip silently
        }
    }

    // 4. Check for uncommitted changes
    try {
        const status = execSync('git status --porcelain 2>/dev/null', {
            encoding: 'utf-8',
            timeout: 3000,
            cwd,
        }).trim();
        if (status) {
            const fileCount = status.split('\n').length;
            lines.push(`[Delimit] ⚠ ${fileCount} uncommitted file(s) — consider committing before deploy`);
        }
    } catch { /* not a git repo */ }

    // 5. Result
    lines.push('');
    if (blocked) {
        lines.push('[Delimit] ✗ DEPLOY BLOCKED — fix import errors above');
        lines.push('[Delimit] Run: delimit_test_smoke for full diagnostics');
    } else {
        lines.push('[Delimit] ✓ Deploy gate passed');
    }
    lines.push('');

    process.stdout.write(lines.join('\n') + '\n');

    if (blocked) {
        process.exit(1);
    }
}

// ---------------------------------------------------------------------------

module.exports = {
    detectAITools,
    installHooksForTool,
    installAllHooks,
    installClaudeHooks,
    installCodexHooks,
    installGeminiHooks,
    installAntigravityHooks,
    removeAllHooks,
    removeClaudeHooks,
    removeCodexHooks,
    removeGeminiHooks,
    removeAntigravityHooks,
    loadHookConfig,
    hookSessionStart,
    hookBootstrap,
    hookPreTool,
    hookPreCommit,
    hookDeployGate,
    countPendingStrategyItems,
    getTopStrategyItem,
    findClaudeHookGroup,
    migrateToNestedFormat,
};
