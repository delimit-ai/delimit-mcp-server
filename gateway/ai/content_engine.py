"""Autonomous content engine for Delimit.

Generates terminal demo videos, uploads to YouTube, and manages tweet scheduling.
All content is value-first: tutorials, governance insights, and real demos.

Components:
  - Cast generator: scripted terminal demos via asciinema
  - Video renderer: HTML + puppeteer CDP screencast + ffmpeg compositing
  - YouTube uploader: OAuth2 upload via google-api-python-client
  - Tweet scheduler: queue-based tweet posting via tweepy
  - Cron orchestrator: ties it all together on a schedule
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.content_engine")

SECRETS_DIR = Path.home() / ".delimit" / "secrets"
CONTENT_DIR = Path.home() / ".delimit" / "content"
CONTENT_LOG = Path.home() / ".delimit" / "content_log.jsonl"
TWEET_QUEUE = Path.home() / ".delimit" / "tweet_queue.json"
TWEET_SCHEDULE = Path.home() / ".delimit" / "content" / "tweet_schedule.json"
VIDEO_QUEUE = Path.home() / ".delimit" / "video_queue.json"
ASSETS_DIR = CONTENT_DIR / "assets"
VIDEOS_DIR = CONTENT_DIR / "videos"
CASTS_DIR = CONTENT_DIR / "casts"


# ═══════════════════════════════════════════════════════════════════════
#  VIDEO SCRIPTS — each is a scripted terminal demo
# ═══════════════════════════════════════════════════════════════════════

VIDEO_SCRIPTS = {
    "install": {
        "title": "Install Delimit in 60 Seconds",
        "description": "Set up API governance for your AI coding workflow in under a minute. Delimit catches breaking API changes before they reach production.",
        "tags": ["delimit", "mcp", "ai governance", "claude code", "codex", "api governance", "openapi"],
        "category": "28",
        "commands": [
            ("echo '# Install Delimit CLI'", 1.5),
            ("npx delimit-cli@latest init --preset default", 4),
            ("echo ''", 0.5),
            ("echo '# Check your governance setup'", 1.5),
            ("npx delimit-cli@latest doctor", 3),
            ("echo ''", 0.5),
            ("echo '# Done! Breaking changes will be caught automatically.'", 2),
        ],
        "duration_estimate": 60,
    },
    "breaking_changes": {
        "title": "Catch Breaking API Changes Before Merge",
        "description": "Detect 23 types of breaking changes in OpenAPI specs automatically. No configuration needed.",
        "tags": ["openapi", "api", "breaking changes", "github action", "ci cd", "api governance"],
        "category": "28",
        "commands": [
            ("echo '# Detecting breaking API changes with Delimit'", 1.5),
            ("echo '# Lets compare two versions of an API spec'", 1.5),
            ("cat api/v1.yaml | head -20", 2),
            ("echo ''", 0.5),
            ("echo '# Now lint for breaking changes'", 1.5),
            ("npx delimit-cli@latest lint --old api/v1.yaml --new api/v2.yaml", 4),
            ("echo ''", 0.5),
            ("echo '# 3 breaking changes caught before merge!'", 2),
        ],
        "duration_estimate": 75,
    },
    "github_action": {
        "title": "Add API Governance to Any Repo in 30 Seconds",
        "description": "One YAML file. Zero configuration. Automatic breaking change detection on every PR.",
        "tags": ["github actions", "ci cd", "api governance", "openapi", "pull request"],
        "category": "28",
        "commands": [
            ("echo '# Add Delimit to your GitHub Actions workflow'", 1.5),
            ("echo '# Just add this to .github/workflows/api-check.yml:'", 1.5),
            ("cat <<'EOF'\nname: API Governance\non: [pull_request]\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: delimit-ai/delimit-action@v1\n        with:\n          spec: api/openapi.yaml\nEOF", 3),
            ("echo ''", 0.5),
            ("echo '# Thats it. Every PR now gets breaking change detection.'", 2),
        ],
        "duration_estimate": 50,
    },
    "policy_presets": {
        "title": "3 Policy Presets for API Governance",
        "description": "Strict, default, or relaxed. Match your API governance to your team's risk tolerance.",
        "tags": ["api governance", "policy", "openapi", "breaking changes", "configuration"],
        "category": "28",
        "commands": [
            ("echo '# Delimit policy presets'", 1.5),
            ("echo ''", 0.3),
            ("echo '# Strict: every violation is an error'", 1.5),
            ("npx delimit-cli@latest init --preset strict", 3),
            ("echo ''", 0.5),
            ("echo '# Default: balanced warnings and errors'", 1.5),
            ("npx delimit-cli@latest init --preset default", 3),
            ("echo ''", 0.5),
            ("echo '# Relaxed: warnings only, nothing blocks'", 1.5),
            ("npx delimit-cli@latest init --preset relaxed", 3),
            ("echo ''", 0.5),
            ("echo '# Pick the one that fits your team.'", 2),
        ],
        "duration_estimate": 70,
    },
    "multi_model": {
        "title": "One Workspace for Every AI Coding Assistant",
        "description": "Switch between Claude Code, Codex, and Gemini CLI without losing context. Your governance rules follow you.",
        "tags": ["claude code", "codex", "gemini", "ai coding", "mcp", "context"],
        "category": "28",
        "commands": [
            ("echo '# The problem: context loss when switching AI assistants'", 1.5),
            ("echo '# Claude Code knows your API spec...'", 1.5),
            ("echo '# But Codex starts from zero.'", 1.5),
            ("echo ''", 0.5),
            ("echo '# Delimit solves this with a shared governance layer.'", 1.5),
            ("echo '# Your policies, your spec, your rules -- everywhere.'", 2),
            ("echo ''", 0.5),
            ("echo '# Setup:'", 1),
            ("npx delimit-cli@latest init", 3),
            ("echo ''", 0.5),
            ("echo '# Now every assistant sees the same governance rules.'", 2),
        ],
        "duration_estimate": 65,
    },
    "diff_engine": {
        "title": "23 Change Types Detected Automatically",
        "description": "Delimit's diff engine classifies every API change: endpoints, parameters, schemas, security, and more.",
        "tags": ["api diff", "openapi", "change detection", "semver", "api versioning"],
        "category": "28",
        "commands": [
            ("echo '# Delimit detects 23 types of API changes'", 1.5),
            ("echo '# Endpoint added, removed, or renamed'", 1),
            ("echo '# Parameter type changed'", 1),
            ("echo '# Required field added to request body'", 1),
            ("echo '# Response schema modified'", 1),
            ("echo '# Security scheme changed'", 1),
            ("echo '# ...and 17 more'", 1),
            ("echo ''", 0.5),
            ("echo '# Each change gets a semver classification:'", 1.5),
            ("echo '# MAJOR = breaking, MINOR = additive, PATCH = fix'", 2),
            ("echo ''", 0.5),
            ("npx delimit-cli@latest diff --old api/v1.yaml --new api/v2.yaml", 4),
        ],
        "duration_estimate": 70,
    },
    "zero_config": {
        "title": "Zero Configuration API Governance",
        "description": "Delimit works out of the box. No YAML to write, no rules to configure. Just point it at your spec.",
        "tags": ["zero config", "api governance", "openapi", "developer experience", "dx"],
        "category": "28",
        "commands": [
            ("echo '# Zero config API governance'", 1.5),
            ("echo '# Step 1: You have an OpenAPI spec'", 1.5),
            ("ls api/", 2),
            ("echo ''", 0.5),
            ("echo '# Step 2: Run delimit lint'", 1.5),
            ("npx delimit-cli@latest lint --old api/v1.yaml --new api/v2.yaml", 4),
            ("echo ''", 0.5),
            ("echo '# Thats it. No config files. No setup. It just works.'", 2),
        ],
        "duration_estimate": 55,
    },
    "ai_agents": {
        "title": "Why AI Agents Need API Governance",
        "description": "AI coding agents generate API changes fast. Without governance, breaking changes ship to production unchecked.",
        "tags": ["ai agents", "api governance", "ai safety", "code review", "automation"],
        "category": "28",
        "commands": [
            ("echo '# AI agents are writing code faster than humans can review'", 2),
            ("echo '# They generate API changes in seconds'", 1.5),
            ("echo '# But who checks if those changes break consumers?'", 2),
            ("echo ''", 0.5),
            ("echo '# Delimit is the governance layer for AI-generated APIs'", 2),
            ("echo ''", 0.5),
            ("echo '# Add one GitHub Action:'", 1.5),
            ("echo '  - uses: delimit-ai/delimit-action@v1'", 1),
            ("echo '    with:'", 0.5),
            ("echo '      spec: api/openapi.yaml'", 1),
            ("echo ''", 0.5),
            ("echo '# Every AI-generated PR gets breaking change detection.'", 2),
            ("echo '# Governance isnt optional. Its infrastructure.'", 2),
        ],
        "duration_estimate": 75,
    },
}


# ═══════════════════════════════════════════════════════════════════════
#  CAST GENERATOR — scripted terminal demos via asciinema format
# ═══════════════════════════════════════════════════════════════════════

def generate_cast(script_id: str, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Generate an asciinema .cast file from a video script.

    Creates a v2 asciicast format file with synthetic typing and output.
    Does NOT actually run the commands -- generates realistic-looking output.
    """
    if script_id not in VIDEO_SCRIPTS:
        return {"error": f"Unknown script: {script_id}", "available": list(VIDEO_SCRIPTS.keys())}

    script = VIDEO_SCRIPTS[script_id]
    CASTS_DIR.mkdir(parents=True, exist_ok=True)

    if not output_path:
        output_path = str(CASTS_DIR / f"{script_id}.cast")

    # v2 asciicast header
    header = {
        "version": 2,
        "width": 100,
        "height": 30,
        "timestamp": int(time.time()),
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
        "title": script["title"],
    }

    events = []
    current_time = 0.5  # start after half second

    for cmd_text, pause_after in script["commands"]:
        # Type the command character by character (simulated typing speed)
        prompt = "$ "
        events.append([current_time, "o", f"\r\n\x1b[1;32m{prompt}\x1b[0m"])
        current_time += 0.1

        for char in cmd_text:
            events.append([round(current_time, 3), "o", char])
            current_time += 0.04  # 40ms per character = realistic typing

        # Press enter
        current_time += 0.2
        events.append([round(current_time, 3), "o", "\r\n"])
        current_time += 0.3

        # Generate synthetic output based on command
        output = _synthetic_output(cmd_text, script_id)
        if output:
            for line in output.split("\n"):
                events.append([round(current_time, 3), "o", line + "\r\n"])
                current_time += 0.05

        current_time += pause_after

    # Write the cast file
    with open(output_path, "w") as f:
        f.write(json.dumps(header) + "\n")
        for event in events:
            f.write(json.dumps(event) + "\n")

    return {
        "cast_path": output_path,
        "script_id": script_id,
        "title": script["title"],
        "events": len(events),
        "duration_seconds": round(current_time, 1),
    }


def _synthetic_output(cmd: str, script_id: str) -> str:
    """Generate realistic synthetic output for demo commands."""
    if "delimit-cli@latest init" in cmd or "delimit-cli@latest init" in cmd:
        preset = "default"
        if "--preset strict" in cmd:
            preset = "strict"
        elif "--preset relaxed" in cmd:
            preset = "relaxed"
        return (
            f"\x1b[1;36mdelimit\x1b[0m Initializing project with {preset} preset...\n"
            f"\x1b[1;32m  +\x1b[0m Created .delimit/policies.yml\n"
            f"\x1b[1;32m  +\x1b[0m Created .github/workflows/api-check.yml\n"
            f"\x1b[1;32m  done\x1b[0m Project initialized with {preset} policy preset."
        )
    elif "delimit-cli@latest doctor" in cmd:
        return (
            "\x1b[1;36mdelimit\x1b[0m Running diagnostics...\n"
            "\x1b[1;32m  pass\x1b[0m Policy file found\n"
            "\x1b[1;32m  pass\x1b[0m GitHub Action configured\n"
            "\x1b[1;32m  pass\x1b[0m OpenAPI spec detected\n"
            "\x1b[1;32m  pass\x1b[0m Git repository initialized\n"
            "\x1b[1;36m  4/4 checks passed\x1b[0m"
        )
    elif "delimit-cli@latest lint" in cmd:
        return (
            "\x1b[1;36mdelimit\x1b[0m Linting API changes...\n"
            "\n"
            "\x1b[1;31m  BREAKING\x1b[0m Endpoint removed: DELETE /api/v1/users/{id}\n"
            "\x1b[1;31m  BREAKING\x1b[0m Required field added: POST /api/v1/orders body.shipping_method\n"
            "\x1b[1;31m  BREAKING\x1b[0m Response type changed: GET /api/v1/products[].price (string -> number)\n"
            "\x1b[1;33m  WARNING\x1b[0m Endpoint deprecated: GET /api/v1/legacy/search\n"
            "\n"
            "\x1b[1;31m  3 breaking changes\x1b[0m | \x1b[1;33m1 warning\x1b[0m | Semver: MAJOR"
        )
    elif "delimit-cli@latest diff" in cmd:
        return (
            "\x1b[1;36mdelimit\x1b[0m Diffing API specs...\n"
            "\n"
            "  \x1b[1;31mremoved\x1b[0m  DELETE /api/v1/users/{id}\n"
            "  \x1b[1;31mchanged\x1b[0m  POST /api/v1/orders  (added required field)\n"
            "  \x1b[1;31mchanged\x1b[0m  GET /api/v1/products  (response type changed)\n"
            "  \x1b[1;33madded\x1b[0m    POST /api/v2/users/bulk\n"
            "  \x1b[1;33madded\x1b[0m    GET /api/v2/analytics\n"
            "\n"
            "  \x1b[1;36m5 changes\x1b[0m: 3 breaking, 2 additive | Semver: MAJOR"
        )
    elif cmd.startswith("cat ") and "yaml" in cmd:
        return (
            "openapi: '3.0.3'\n"
            "info:\n"
            "  title: Sample API\n"
            "  version: 1.0.0\n"
            "paths:\n"
            "  /api/v1/users:\n"
            "    get:\n"
            "      summary: List users\n"
            "      responses:\n"
            "        '200':\n"
            "          description: OK"
        )
    elif cmd.startswith("ls api"):
        return "openapi.yaml  v1.yaml  v2.yaml"
    elif cmd.startswith("echo "):
        return ""  # echo commands render their own output
    elif cmd.startswith("cat <<"):
        return ""  # heredoc renders itself

    return ""


# ═══════════════════════════════════════════════════════════════════════
#  VIDEO RENDERER — HTML + puppeteer screencast + ffmpeg
# ═══════════════════════════════════════════════════════════════════════

_TERMINAL_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117;
    width: 1920px;
    height: 1080px;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  .header {
    text-align: center;
    margin-bottom: 24px;
  }
  .logo {
    font-size: 28px;
    font-weight: 700;
    color: #58a6ff;
    letter-spacing: 2px;
  }
  .title {
    font-size: 36px;
    font-weight: 600;
    color: #e6edf3;
    margin-top: 8px;
  }
  .terminal-container {
    background: #161b22;
    border-radius: 12px;
    border: 1px solid #30363d;
    width: 1600px;
    height: 750px;
    overflow: hidden;
    box-shadow: 0 16px 48px rgba(0,0,0,0.4);
  }
  .terminal-header {
    background: #21262d;
    height: 36px;
    display: flex;
    align-items: center;
    padding: 0 16px;
    gap: 8px;
  }
  .dot { width: 12px; height: 12px; border-radius: 50%; }
  .dot-red { background: #f85149; }
  .dot-yellow { background: #d29922; }
  .dot-green { background: #3fb950; }
  .terminal-body {
    padding: 20px;
    font-size: 18px;
    line-height: 1.6;
    color: #e6edf3;
    white-space: pre-wrap;
    overflow: hidden;
    height: 714px;
  }
  .watermark {
    margin-top: 24px;
    font-size: 20px;
    color: #484f58;
    letter-spacing: 1px;
  }
  #player { width: 100%; height: 100%; }
</style>
<link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/asciinema-player@3.7.1/dist/bundle/asciinema-player.css" />
</head>
<body>
  <div class="header">
    <div class="logo">DELIMIT</div>
    <div class="title">{{TITLE}}</div>
  </div>
  <div class="terminal-container">
    <div class="terminal-header">
      <div class="dot dot-red"></div>
      <div class="dot dot-yellow"></div>
      <div class="dot dot-green"></div>
    </div>
    <div class="terminal-body">
      <div id="player"></div>
    </div>
  </div>
  <div class="watermark">delimit.ai</div>

  <script src="https://cdn.jsdelivr.net/npm/asciinema-player@3.7.1/dist/bundle/asciinema-player.min.js"></script>
  <script>
    const player = AsciinemaPlayer.create(
      '{{CAST_URL}}',
      document.getElementById('player'),
      {
        cols: 100,
        rows: 28,
        autoPlay: true,
        speed: 1,
        theme: 'monokai',
        fit: 'width',
        terminalFontFamily: "'SF Mono', 'Fira Code', 'Consolas', monospace",
        terminalFontSize: '18px',
      }
    );
  </script>
</body>
</html>"""


def _generate_ambient_music(output_path: str, duration: int = 120) -> Dict[str, Any]:
    """Generate ambient background music using ffmpeg sine wave synthesis."""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=174:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=261:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=349:duration={duration}",
            "-filter_complex",
            "[0:a][1:a][2:a]amix=inputs=3,lowpass=f=300,volume=0.08",
            "-c:a", "aac", "-b:a", "128k",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {"error": f"ffmpeg failed: {result.stderr[:500]}"}
        return {"path": output_path, "duration": duration}
    except Exception as e:
        return {"error": str(e)}


def _create_puppeteer_script(html_path: str, output_path: str, duration_ms: int) -> str:
    """Create a Node.js puppeteer script for CDP screencast capture."""
    return f"""
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

(async () => {{
    const browser = await puppeteer.launch({{
        headless: 'new',
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--window-size=1920,1080'],
        defaultViewport: {{ width: 1920, height: 1080 }},
    }});

    const page = await browser.newPage();
    await page.goto('file://{html_path}', {{ waitUntil: 'networkidle0', timeout: 30000 }});

    // Wait for asciinema player to load
    await new Promise(r => setTimeout(r, 2000));

    const framesDir = '{output_path}_frames';
    if (!fs.existsSync(framesDir)) fs.mkdirSync(framesDir, {{ recursive: true }});

    const client = await page.createCDPSession();

    let frameCount = 0;
    client.on('Page.screencastFrame', async (params) => {{
        const frameFile = path.join(framesDir, `frame_${{String(frameCount).padStart(6, '0')}}.png`);
        fs.writeFileSync(frameFile, Buffer.from(params.data, 'base64'));
        frameCount++;
        await client.send('Page.screencastFrameAck', {{ sessionId: params.sessionId }});
    }});

    await client.send('Page.startScreencast', {{
        format: 'png',
        quality: 80,
        maxWidth: 1920,
        maxHeight: 1080,
        everyNthFrame: 1,
    }});

    // Record for the duration
    await new Promise(r => setTimeout(r, {duration_ms}));

    await client.send('Page.stopScreencast');
    await browser.close();

    console.log(JSON.stringify({{ frames: frameCount, framesDir }}));
}})();
"""


def render_video(cast_path: str, output_path: str, title: str, duration_seconds: int = 90) -> Dict[str, Any]:
    """Render a .cast file to MP4 via puppeteer screencast + ffmpeg compositing.

    Pipeline:
      1. Create HTML page embedding asciinema player with the cast file
      2. Use puppeteer CDP screencast to capture frames
      3. Generate ambient music track
      4. Combine frames + music with ffmpeg
    """
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="delimit_video_") as tmpdir:
        tmpdir_path = Path(tmpdir)

        # 1. Create HTML page
        html_content = _TERMINAL_HTML_TEMPLATE.replace("{{TITLE}}", title)
        html_content = html_content.replace("{{CAST_URL}}", cast_path)
        html_path = tmpdir_path / "player.html"
        html_path.write_text(html_content)

        # 2. Run puppeteer to capture frames
        puppeteer_script = _create_puppeteer_script(
            str(html_path),
            str(tmpdir_path / "video"),
            duration_seconds * 1000,
        )
        script_path = tmpdir_path / "capture.js"
        script_path.write_text(puppeteer_script)

        try:
            result = subprocess.run(
                ["node", str(script_path)],
                capture_output=True, text=True,
                timeout=duration_seconds + 60,
                env={**os.environ, "NODE_PATH": "/usr/lib/node_modules"},
            )
        except subprocess.TimeoutExpired:
            return {"error": "Puppeteer capture timed out"}

        if result.returncode != 0:
            return {"error": f"Puppeteer failed: {result.stderr[:500]}"}

        try:
            capture_info = json.loads(result.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            return {"error": f"Puppeteer output parse error: {result.stdout[:200]}"}

        frames_dir = capture_info.get("framesDir", "")
        frame_count = capture_info.get("frames", 0)

        if frame_count == 0:
            return {"error": "No frames captured"}

        # 3. Generate ambient music
        music_path = str(ASSETS_DIR / "ambient.m4a")
        if not Path(music_path).exists():
            music_result = _generate_ambient_music(music_path, duration_seconds + 30)
            if "error" in music_result:
                logger.warning("Music generation failed, proceeding without: %s", music_result["error"])
                music_path = None

        # 4. Combine frames into video with ffmpeg
        fps = max(1, frame_count // duration_seconds)
        raw_video = str(tmpdir_path / "raw.mp4")

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", f"{frames_dir}/frame_%06d.png",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "medium",
            "-crf", "23",
            raw_video,
        ]
        sub = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)
        if sub.returncode != 0:
            return {"error": f"ffmpeg frame assembly failed: {sub.stderr[:500]}"}

        # 5. Add music track if available
        if music_path and Path(music_path).exists():
            final_cmd = [
                "ffmpeg", "-y",
                "-i", raw_video,
                "-i", music_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                "-map", "0:v:0",
                "-map", "1:a:0",
                output_path,
            ]
            sub = subprocess.run(final_cmd, capture_output=True, text=True, timeout=60)
            if sub.returncode != 0:
                # Fall back to video without music
                shutil.copy2(raw_video, output_path)
        else:
            shutil.copy2(raw_video, output_path)

    return {
        "video_path": output_path,
        "title": title,
        "frames": frame_count,
        "duration_seconds": duration_seconds,
        "has_music": music_path is not None and Path(music_path).exists(),
    }


def generate_video(script_id: str) -> Dict[str, Any]:
    """Full pipeline: generate cast, render to video."""
    script = VIDEO_SCRIPTS.get(script_id)
    if not script:
        return {"error": f"Unknown script: {script_id}", "available": list(VIDEO_SCRIPTS.keys())}

    # Step 1: Generate cast
    cast_result = generate_cast(script_id)
    if "error" in cast_result:
        return cast_result

    # Step 2: Render video
    output_path = str(VIDEOS_DIR / f"{script_id}.mp4")
    duration = script.get("duration_estimate", 90)

    video_result = render_video(
        cast_result["cast_path"],
        output_path,
        script["title"],
        duration,
    )

    if "error" in video_result:
        # Return partial result with cast but video error
        return {
            "cast": cast_result,
            "video_error": video_result["error"],
        }

    return {
        "cast": cast_result,
        "video": video_result,
        "script": {
            "id": script_id,
            "title": script["title"],
            "description": script["description"],
            "tags": script["tags"],
        },
    }


# ═══════════════════════════════════════════════════════════════════════
#  YOUTUBE UPLOADER — OAuth2 via google-api-python-client
# ═══════════════════════════════════════════════════════════════════════

def _get_youtube_credentials():
    """Load and refresh YouTube OAuth2 credentials."""
    from google.oauth2.credentials import Credentials

    tokens_path = SECRETS_DIR / "youtube-tokens.json"
    client_path = SECRETS_DIR / "youtube-oauth-client.json"

    if not tokens_path.exists():
        return None, "Missing youtube-tokens.json in ~/.delimit/secrets/"
    if not client_path.exists():
        return None, "Missing youtube-oauth-client.json in ~/.delimit/secrets/"

    tokens = json.loads(tokens_path.read_text())
    client_creds = json.loads(client_path.read_text())

    # Handle both 'installed' and 'web' client types
    client_info = client_creds.get("installed", client_creds.get("web", {}))

    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_info.get("client_id"),
        client_secret=client_info.get("client_secret"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    # Refresh if expired
    if creds.expired or not creds.valid:
        from google.auth.transport.requests import Request
        try:
            creds.refresh(Request())
            # Persist refreshed tokens
            tokens["access_token"] = creds.token
            tokens_path.write_text(json.dumps(tokens, indent=2))
        except Exception as e:
            return None, f"Token refresh failed: {e}"

    return creds, None


def test_youtube_auth() -> Dict[str, Any]:
    """Test YouTube OAuth token refresh without uploading.

    Note: The stored tokens may only have youtube.upload scope, so listing
    channels may fail with 403. That is fine -- a successful token refresh
    means uploads will work. We treat scope errors as authenticated.
    """
    creds, error = _get_youtube_credentials()
    if error:
        return {"authenticated": False, "error": error}

    try:
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", credentials=creds)
        # Try listing channels to get channel name
        response = youtube.channels().list(part="snippet", mine=True).execute()
        channels = response.get("items", [])
        if channels:
            return {
                "authenticated": True,
                "channel": channels[0]["snippet"]["title"],
                "channel_id": channels[0]["id"],
            }
        return {"authenticated": True, "channel": "unknown (no channels found)"}
    except Exception as e:
        error_str = str(e)
        # 403 insufficient scopes means auth works but token scope is limited
        # This is expected when tokens only have youtube.upload scope
        if "insufficientPermissions" in error_str or "authentication scopes" in error_str:
            return {
                "authenticated": True,
                "note": "Token valid but limited to upload scope (channels.list requires youtube.readonly)",
                "token_valid": creds.token is not None,
            }
        return {"authenticated": False, "error": error_str}


def upload_to_youtube(video_path: str, title: str, description: str,
                      tags: List[str], category: str = "28",
                      privacy: str = "public") -> Dict[str, Any]:
    """Upload a video to YouTube via OAuth2.

    Args:
        video_path: Path to the MP4 file.
        title: Video title.
        description: Video description (CTA appended automatically).
        tags: List of tag strings.
        category: YouTube category ID (28 = Science & Technology).
        privacy: public, unlisted, or private.

    Returns:
        Dict with video_id and url on success, or error.
    """
    if not Path(video_path).exists():
        return {"error": f"Video file not found: {video_path}"}

    creds, error = _get_youtube_credentials()
    if error:
        return {"error": error}

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        youtube = build("youtube", "v3", credentials=creds)

        # Append CTA to description
        full_description = (
            f"{description}\n\n"
            "Get started:\n"
            "  npx delimit-cli@latest init\n\n"
            "GitHub Action:\n"
            "  https://github.com/marketplace/actions/delimit-api-governance\n\n"
            "Docs: https://delimit.ai/docs\n"
            "GitHub: https://github.com/delimit-ai/delimit-mcp-server"
        )

        body = {
            "snippet": {
                "title": title,
                "description": full_description,
                "tags": tags,
                "categoryId": category,
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        # Resumable upload with progress tracking
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("Upload progress: %d%%", int(status.progress() * 100))

        video_id = response["id"]
        result = {
            "uploaded": True,
            "video_id": video_id,
            "url": f"https://youtube.com/watch?v={video_id}",
            "title": title,
            "privacy": privacy,
        }

        _log_content_event("youtube_upload", result)
        return result

    except Exception as e:
        return {"error": f"YouTube upload failed: {e}"}


# ═══════════════════════════════════════════════════════════════════════
#  TWEET SCHEDULER — queue-based tweet management
# ═══════════════════════════════════════════════════════════════════════

def _load_tweet_queue() -> List[Dict[str, Any]]:
    """Load the tweet queue from disk."""
    if not TWEET_QUEUE.exists():
        return []
    try:
        return json.loads(TWEET_QUEUE.read_text())
    except (json.JSONDecodeError, ValueError):
        return []


def _save_tweet_queue(queue: List[Dict[str, Any]]):
    """Save the tweet queue to disk."""
    TWEET_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    TWEET_QUEUE.write_text(json.dumps(queue, indent=2))


def add_tweets_to_queue(tweets: List[str]) -> Dict[str, Any]:
    """Add tweets to the posting queue.

    Args:
        tweets: List of tweet text strings to queue.
    """
    queue = _load_tweet_queue()
    added = 0
    for text in tweets:
        text = text.strip() if text else ""
        if not text or len(text) > 280:
            continue
        # Deduplicate by text content
        existing_texts = {t["text"] for t in queue}
        if text not in existing_texts:
            queue.append({
                "text": text,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "posted": False,
                "posted_at": None,
                "tweet_id": None,
            })
            added += 1

    _save_tweet_queue(queue)
    return {
        "added": added,
        "queue_size": len([t for t in queue if not t["posted"]]),
        "total": len(queue),
    }


def get_next_tweet() -> Optional[Dict[str, Any]]:
    """Get the next unposted tweet from the queue."""
    queue = _load_tweet_queue()
    for tweet in queue:
        if not tweet.get("posted"):
            return tweet
    return None


def _load_tweet_schedule() -> Dict[str, Any]:
    """Load the tweet schedule from disk."""
    if not TWEET_SCHEDULE.exists():
        return {}
    try:
        return json.loads(TWEET_SCHEDULE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def _save_tweet_schedule(schedule: Dict[str, Any]):
    """Save the tweet schedule to disk."""
    TWEET_SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
    TWEET_SCHEDULE.write_text(json.dumps(schedule, indent=2))


def get_scheduled_tweet() -> Optional[Dict[str, Any]]:
    """Get today's scheduled tweet based on day of week and current week rotation.

    Reads the tweet schedule, determines the current day (ET timezone) and
    the current week (cycling through weeks by ISO week number modulo total
    weeks), and returns the matching tweet entry.

    Returns None if:
      - No schedule file exists
      - Today is a rest day (no tweet scheduled)
      - The tweet has status "skip_if_no_news"
      - The tweet was already posted
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # Python <3.9

    schedule = _load_tweet_schedule()
    weeks = schedule.get("weeks", [])
    if not weeks:
        return None

    # Determine today in ET
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    day_name = now_et.strftime("%A").lower()  # monday, tuesday, ...

    # Cycle through weeks using ISO week number
    iso_week = now_et.isocalendar()[1]
    week_index = iso_week % len(weeks)
    current_week = weeks[week_index]

    # Find today's tweet in the current week
    today_tweet = None
    today_index = None
    for idx, tweet in enumerate(current_week.get("tweets", [])):
        if tweet.get("day") == day_name:
            today_tweet = tweet
            today_index = idx
            break

    if today_tweet is None:
        return None

    # Skip if already posted
    if today_tweet.get("status") == "posted":
        return None

    # Skip if conditional and no news
    if today_tweet.get("status") == "skip_if_no_news":
        return None

    # Build the return dict
    result = {
        "text": today_tweet.get("text", ""),
        "day_type": today_tweet.get("day_type", ""),
        "media_type": today_tweet.get("media_type", "none"),
        "media_note": today_tweet.get("media_note", ""),
        "is_thread": today_tweet.get("is_thread", False),
        "thread_tweets": today_tweet.get("thread_tweets", []),
        "youtube_link_in_reply": today_tweet.get("youtube_link_in_reply", False),
    }
    if today_tweet.get("video_script"):
        result["video_script"] = today_tweet["video_script"]

    # Mark as posted in the schedule file
    current_week["tweets"][today_index]["status"] = "posted"
    current_week["tweets"][today_index]["posted_at"] = datetime.now(timezone.utc).isoformat()
    _save_tweet_schedule(schedule)

    return result


def post_next_tweet() -> Dict[str, Any]:
    """Post the next scheduled or queued tweet via the Twitter API.

    Checks the day-typed tweet schedule first. Falls back to the flat queue
    if no scheduled tweet is available for today.
    """
    from ai.social import post_tweet, should_post_now
    from ai.posting_budget import (
        DAILY_POST_CAP,
        cap_reached,
        category_priority,
        posts_today,
    )

    # HARD global daily cap (2026-06-19, founder-ratified): at most
    # DAILY_POST_CAP brand tweets ACTUALLY POSTED per UTC day, shared across
    # all three autopost sources (ship_event, vendor_news_riff,
    # scheduled_original). Counted from the posted-tweet log, not the queue.
    # This is a backstop ON TOP OF should_post_now's 24/day sliding window.
    if cap_reached():
        return {
            "status": "skipped",
            "reason": f"Global daily post cap reached ({posts_today()}/{DAILY_POST_CAP})",
            "posts_today": posts_today(),
            "daily_post_cap": DAILY_POST_CAP,
        }

    if not should_post_now():
        return {"status": "skipped", "reason": "Rate cap hit (2/hr or 24/day)"}

    # --- Try day-typed schedule first ---
    scheduled = get_scheduled_tweet()
    if scheduled:
        if scheduled.get("is_thread") and scheduled.get("thread_tweets"):
            # Post thread: first tweet, then replies
            tweets_to_post = scheduled["thread_tweets"]
            first_result = post_tweet(tweets_to_post[0])
            if "error" in first_result:
                return first_result
            parent_id = first_result.get("id", "")
            posted_ids = [parent_id]
            for reply_text in tweets_to_post[1:]:
                reply_result = post_tweet(reply_text, reply_to_id=parent_id)
                if "error" not in reply_result:
                    parent_id = reply_result.get("id", "")
                    posted_ids.append(parent_id)
            _log_content_event("scheduled_thread_posted", {
                "day_type": scheduled["day_type"],
                "tweet_count": len(tweets_to_post),
                "tweet_ids": posted_ids,
            })
            return {
                "status": "posted",
                "source": "schedule",
                "day_type": scheduled["day_type"],
                "is_thread": True,
                "tweet_ids": posted_ids,
            }
        else:
            # Single scheduled tweet
            result = post_tweet(scheduled["text"])
            if "error" not in result:
                _log_content_event("scheduled_tweet_posted", {
                    "text": scheduled["text"][:100],
                    "day_type": scheduled["day_type"],
                    "tweet_id": result.get("id"),
                })
                return {
                    **result,
                    "source": "schedule",
                    "day_type": scheduled["day_type"],
                }
            return result

    # --- Fall back to flat queue (priority-ordered) ---
    # Pick the highest-priority unposted entry: ship_event + vendor_news_riff
    # are P0, scheduled_original is P2 (panel guardrail). Ties keep insertion
    # order (stable) so the existing top-of-queue P0 insert still wins within
    # a category. We index the original list so we mutate/save in place.
    queue = _load_tweet_queue()
    unposted = [
        (category_priority(t.get("category", "")), idx, t)
        for idx, t in enumerate(queue)
        if not t.get("posted")
    ]
    if unposted:
        unposted.sort(key=lambda x: (x[0], x[1]))  # priority, then queue order
        _prio, i, tweet = unposted[0]
        result = post_tweet(tweet["text"])
        if "error" not in result:
            queue[i]["posted"] = True
            queue[i]["posted_at"] = datetime.now(timezone.utc).isoformat()
            queue[i]["tweet_id"] = result.get("id")
            _save_tweet_queue(queue)
            _log_content_event("tweet_posted", {
                "text": tweet["text"][:100],
                "tweet_id": result.get("id"),
                "category": tweet.get("category", ""),
            })
        return result

    return {"status": "empty", "reason": "No scheduled or queued tweets available"}


def get_tweet_queue_status() -> Dict[str, Any]:
    """Get current tweet queue status."""
    queue = _load_tweet_queue()
    pending = [t for t in queue if not t.get("posted")]
    posted = [t for t in queue if t.get("posted")]
    return {
        "pending": len(pending),
        "posted": len(posted),
        "total": len(queue),
        "next_tweet": pending[0]["text"][:100] if pending else None,
    }


# ═══════════════════════════════════════════════════════════════════════
#  VIDEO QUEUE — manage scheduled video generation/uploads
# ═══════════════════════════════════════════════════════════════════════

def _load_video_queue() -> List[Dict[str, Any]]:
    """Load the video queue from disk."""
    if not VIDEO_QUEUE.exists():
        return []
    try:
        return json.loads(VIDEO_QUEUE.read_text())
    except (json.JSONDecodeError, ValueError):
        return []


def _save_video_queue(queue: List[Dict[str, Any]]):
    """Save the video queue to disk."""
    VIDEO_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    VIDEO_QUEUE.write_text(json.dumps(queue, indent=2))


def populate_video_queue() -> Dict[str, Any]:
    """Populate the video queue with all available scripts that haven't been uploaded."""
    queue = _load_video_queue()
    existing_ids = {v["script_id"] for v in queue}
    added = 0

    for script_id, script in VIDEO_SCRIPTS.items():
        if script_id not in existing_ids:
            queue.append({
                "script_id": script_id,
                "title": script["title"],
                "added_at": datetime.now(timezone.utc).isoformat(),
                "generated": False,
                "uploaded": False,
                "video_path": None,
                "video_id": None,
                "video_url": None,
            })
            added += 1

    _save_video_queue(queue)
    return {"added": added, "total": len(queue)}


def get_next_video() -> Optional[Dict[str, Any]]:
    """Get the next video to generate or upload."""
    queue = _load_video_queue()
    # First: find one that is generated but not uploaded
    for v in queue:
        if v.get("generated") and not v.get("uploaded"):
            return v
    # Then: find one that hasn't been generated
    for v in queue:
        if not v.get("generated"):
            return v
    return None


def process_next_video() -> Dict[str, Any]:
    """Generate and/or upload the next video in the queue."""
    queue = _load_video_queue()

    for i, entry in enumerate(queue):
        # Upload if generated but not uploaded
        if entry.get("generated") and not entry.get("uploaded"):
            script = VIDEO_SCRIPTS.get(entry["script_id"])
            if not script:
                continue
            upload_result = upload_to_youtube(
                entry["video_path"],
                script["title"],
                script["description"],
                script["tags"],
                script.get("category", "28"),
            )
            if "error" not in upload_result:
                queue[i]["uploaded"] = True
                queue[i]["video_id"] = upload_result.get("video_id")
                queue[i]["video_url"] = upload_result.get("url")
                _save_video_queue(queue)
            return {"action": "uploaded", "result": upload_result}

        # Generate if not generated
        if not entry.get("generated"):
            gen_result = generate_video(entry["script_id"])
            if "video" in gen_result and "error" not in gen_result.get("video", {}):
                queue[i]["generated"] = True
                queue[i]["video_path"] = gen_result["video"]["video_path"]
                _save_video_queue(queue)
                return {"action": "generated", "result": gen_result}
            return {"action": "generation_failed", "result": gen_result}

    return {"action": "none", "reason": "All videos generated and uploaded"}


# ═══════════════════════════════════════════════════════════════════════
#  CONTENT SCHEDULE — view upcoming content
# ═══════════════════════════════════════════════════════════════════════

def get_content_schedule() -> Dict[str, Any]:
    """Get the full content schedule: tweets, videos, and history."""
    tweet_status = get_tweet_queue_status()
    video_queue = _load_video_queue()

    pending_videos = [v for v in video_queue if not v.get("uploaded")]
    uploaded_videos = [v for v in video_queue if v.get("uploaded")]

    # Recent content log
    recent_log = []
    if CONTENT_LOG.exists():
        lines = CONTENT_LOG.read_text().strip().split("\n")
        for line in reversed(lines[-20:]):
            try:
                recent_log.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass

    # Tweet schedule calendar info
    tweet_schedule = _load_tweet_schedule()
    schedule_info = None
    if tweet_schedule.get("weeks"):
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        now_et = datetime.now(et)
        iso_week = now_et.isocalendar()[1]
        total_weeks = len(tweet_schedule["weeks"])
        week_index = iso_week % total_weeks
        current_week = tweet_schedule["weeks"][week_index]
        day_name = now_et.strftime("%A").lower()
        today_entry = None
        for t in current_week.get("tweets", []):
            if t.get("day") == day_name:
                today_entry = t
                break
        pending_scheduled = sum(
            1 for w in tweet_schedule["weeks"]
            for t in w.get("tweets", [])
            if t.get("status") == "pending"
        )
        schedule_info = {
            "total_weeks": total_weeks,
            "current_week": current_week.get("week", week_index + 1),
            "today": day_name,
            "today_day_type": today_entry.get("day_type") if today_entry else None,
            "today_status": today_entry.get("status") if today_entry else "rest",
            "pending_scheduled": pending_scheduled,
        }

    return {
        "tweets": tweet_status,
        "tweet_schedule": schedule_info,
        "videos": {
            "pending": len(pending_videos),
            "uploaded": len(uploaded_videos),
            "next": pending_videos[0] if pending_videos else None,
        },
        "schedule": {
            "tweets": "1x daily via day-typed schedule (9:30am ET), flat queue fallback",
            "youtube": "1x weekly (Tuesday 10am ET)",
        },
        "recent_activity": recent_log[:10],
    }


# ═══════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════

def _log_content_event(event_type: str, data: Dict[str, Any]):
    """Log a content engine event to the JSONL log."""
    CONTENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    with open(CONTENT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ═══════════════════════════════════════════════════════════════════════
#  SEED DATA — pre-populate tweet queue with value-first content
# ═══════════════════════════════════════════════════════════════════════

SEED_TWEETS = [
    "API governance tip: The 3 most common breaking changes we catch:\n\n1. Endpoint removed without deprecation\n2. Required field added to request body\n3. Response field type changed\n\nAll detectable before merge.\n\nnpx delimit-cli init",
    "Your CI pipeline checks code quality, test coverage, and security.\n\nBut does it check if your API changes break consumers?\n\nOne line of YAML:\n  - uses: delimit-ai/delimit-action@v1\n    with:\n      spec: api/openapi.yaml",
    "AI coding agents generate API changes faster than humans can review.\n\nThat is exactly why you need automated governance.\n\nDelimit catches breaking changes on every PR -- whether a human or an AI wrote the code.\n\ndelimit.ai",
    "Quick tip: Run `npx delimit-cli doctor` in any project to check your governance setup.\n\nIt checks for policies, specs, workflows, and git config in seconds.",
    "The problem with AI coding assistants is not capability. It is context loss.\n\nEvery time you switch from Claude to Codex to Gemini, you start from zero.\n\nThat is the real productivity killer.\n\ndelimit.ai -- one workspace for every assistant.",
    "Hot take: In 2 years, unmanaged AI agents touching production code will be as unacceptable as unmanaged SSH keys.\n\nGovernance is not optional. It is infrastructure.",
    "Use policy presets to match your team's risk tolerance:\n\n- strict: all violations are errors\n- default: balanced\n- relaxed: warnings only\n\nnpx delimit-cli init --preset strict",
    "We built Delimit because we got tired of breaking changes slipping through code review.\n\n23 change types detected. Automatic semver classification. Zero config.\n\nGitHub Action: github.com/marketplace/actions/delimit-api-governance",
    "What is your API governance process today?\n\nManual review? CI check? Nothing?\n\nNo judgment -- that is why we built this.\n\nnpx delimit-cli init",
    "Delimit detects 23 types of API changes automatically:\n\n- Endpoints added, removed, renamed\n- Parameter types changed\n- Required fields added\n- Response schemas modified\n- Security schemes changed\n\nEach gets a semver classification: MAJOR, MINOR, or PATCH.",
]


def seed_tweet_queue() -> Dict[str, Any]:
    """Seed the tweet queue with value-first content."""
    return add_tweets_to_queue(SEED_TWEETS)
