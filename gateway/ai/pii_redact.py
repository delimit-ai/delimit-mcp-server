"""PII and secret redaction — sanitize before sending to external LLMs (STR-055).

Auto-detect and redact secrets, API keys, and PII before prompts
leave the local environment. Replacement tokens allow reconstruction
if needed.

Focus group (Security): "Auto-detect and redact secrets, API keys, PII
before sending prompt to external LLM. This is a massive security win."
"""

import re
import json
import hashlib
from typing import Any, Dict, List, Optional, Tuple

# Patterns for sensitive data detection
PATTERNS = {
    "api_key": [
        (r'\b(sk-[a-zA-Z0-9]{20,})\b', "OpenAI API key"),
        (r'\b(xai-[a-zA-Z0-9]{20,})\b', "xAI API key"),
        (r'\b(AIza[a-zA-Z0-9_-]{30,})\b', "Google API key"),
        (r'\b(ghp_[a-zA-Z0-9]{36,})\b', "GitHub PAT"),
        (r'\b(ghu_[a-zA-Z0-9]{36,})\b', "GitHub user token"),
        (r'\b(glpat-[a-zA-Z0-9_-]{20,})\b', "GitLab PAT"),
        (r'\b(npm_[a-zA-Z0-9]{36,})\b', "npm token"),
        (r'\b(pypi-[a-zA-Z0-9]{50,})\b', "PyPI token"),
        (r'\b((?:AKIA|ASIA)[0-9A-Z]{16})\b', "AWS access key id"),
    ],
    "secret": [
        # ["']? lets a quoted key match too ("api_key": "...", 'password': '...').
        (r'(?i)(password|passwd|pwd)["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', "password"),
        (r'(?i)(secret|token|api_key|apikey|access_key|private_key|auth_key)["\']?\s*[=:]\s*["\']([^"\']{8,})["\']', "secret/token"),
        (r'(?i)bearer\s+([a-zA-Z0-9._-]{20,})', "bearer token"),
        # A literal default in an env lookup is a hardcoded secret, and a diff
        # that REMOVES one still carries it on its "-" lines. Only the literal
        # (named group "v") is replaced, so the variable name stays readable.
        (r'''(?i)(?:getenv|environ\.get|environ\.setdefault)\(\s*["'][A-Za-z0-9_]*(?:key|secret|token|passw(?:or)?d|pwd|credential)[A-Za-z0-9_]*["']\s*,\s*["'](?P<v>[^"'\s]{8,})["']''', "secret env default"),
        (r'''(?i)process\.env\.[A-Za-z0-9_]*(?:key|secret|token|passw(?:or)?d|pwd|credential)[A-Za-z0-9_]*\s*(?:\|\||\?\?)\s*["'`](?P<v>[^"'`\s]{8,})["'`]''', "secret env default"),
        (r'''(?i)\$\{[A-Za-z0-9_]*(?:key|secret|token|passw(?:or)?d|pwd|credential)[A-Za-z0-9_]*:?-(?P<v>[^}\s]{8,})\}''', "secret env default"),
        # AWS secret keys carry no distinctive prefix; bind them to their name.
        (r'''(?i)aws_secret_access_key["']?\s*[=:]\s*["']?(?P<v>[A-Za-z0-9/+=]{20,})''', "AWS secret access key"),
        # A PEM private key block, whatever the algorithm; the whole block goes.
        # END must repeat the BEGIN label, and the body cannot cross another
        # BEGIN marker, so repeated unterminated BEGINs stay linear (#659 review).
        (r'-----BEGIN (?P<pem>[A-Z0-9 ]{0,40}PRIVATE KEY)-----(?:(?!-----BEGIN )[\s\S]){0,20000}?-----END (?P=pem)-----', "private key block"),
    ],
    "pii": [
        (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', "email"),
        (r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', "phone number"),
        (r'\b\d{3}-\d{2}-\d{4}\b', "SSN"),
        (r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', "credit card"),
    ],
    "infra": [
        (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', "IP address"),
        (r'(?i)(mongodb|postgres|mysql|redis)://[^\s]+', "database URL"),
        (r'(?i)https?://[^\s]*:(password|secret|token)[^\s]*', "URL with credentials"),
    ],
}

# Allowlist — patterns that look like secrets but aren't
ALLOWLIST = [
    r'0\.0\.0\.0',
    r'127\.0\.0\.1',
    r'localhost',
    r'example\.com',
    r'test@test\.com',
    r'placeholder',
    r'REDACTED',
    r'<your-',
    r'\$\{',  # Template variables
]


def _is_allowlisted(match: str) -> bool:
    for pattern in ALLOWLIST:
        if re.search(pattern, match, re.IGNORECASE):
            return True
    return False


def _make_token(category: str, index: int) -> str:
    return f"[REDACTED_{category.upper()}_{index}]"


def redact(
    text: str,
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Redact sensitive data from text.

    Returns the redacted text and a mapping of tokens to original values
    (stored locally, never sent externally).

    Args:
        text: Text to scan and redact.
        categories: Which categories to scan (api_key, secret, pii, infra).
                    None = scan all.
    """
    if not text:
        return {"redacted": "", "findings": [], "token_count": 0}

    active_categories = categories or list(PATTERNS.keys())
    findings = []
    token_map = {}
    redacted = text
    token_index = 0

    for category in active_categories:
        if category not in PATTERNS:
            continue

        for pattern, label in PATTERNS[category]:
            for match in re.finditer(pattern, redacted):
                # Patterns with a "v" group redact only that value.
                matched_text = match.group("v") if "v" in match.re.groupindex else match.group(0)

                if _is_allowlisted(matched_text):
                    continue

                token_index += 1
                token = _make_token(category, token_index)

                findings.append({
                    "category": category,
                    "type": label,
                    "token": token,
                    "position": match.start(),
                    "length": len(matched_text),
                    "preview": matched_text[:4] + "..." + matched_text[-4:] if len(matched_text) > 12 else "***",
                })

                token_map[token] = matched_text
                redacted = redacted.replace(matched_text, token, 1)

    return {
        "redacted": redacted,
        "findings": findings,
        "token_count": token_index,
        "token_map": token_map,  # Keep local — never send externally
        "categories_scanned": active_categories,
    }


def scan(text: str) -> Dict[str, Any]:
    """Scan text for sensitive data WITHOUT redacting.

    Use this to preview what would be redacted.
    """
    result = redact(text)
    return {
        "findings": result["findings"],
        "total": result["token_count"],
        "categories": list(set(f["category"] for f in result["findings"])),
        "safe": result["token_count"] == 0,
        "message": f"Found {result['token_count']} sensitive item(s)" if result["token_count"] > 0 else "No sensitive data detected",
    }


def restore(redacted_text: str, token_map: Dict[str, str]) -> str:
    """Restore redacted text using the token map."""
    result = redacted_text
    for token, original in token_map.items():
        result = result.replace(token, original)
    return result
