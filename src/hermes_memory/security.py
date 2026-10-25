"""Threat scan for memory entries.

Entries are injected into every future system prompt, so anything that could steer the model,
leak a credential, or hide text from the user is refused before it is written.
"""

from __future__ import annotations

import re

_INJECTION = [
    r"\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|messages)",
    r"\bdisregard\s+(the\s+)?(system|previous|above)\b",
    r"\byou\s+are\s+now\s+(an?\s+)?\w+",
    r"\b(new|override|updated)\s+system\s+prompt\b",
    r"\bdo\s+not\s+tell\s+the\s+user\b",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>",
]

_SECRETS = [
    r"\bsk-ant-[A-Za-z0-9_\-]{16,}",
    r"\bsk-(proj-)?[A-Za-z0-9]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bxox[abpr]-[A-Za-z0-9\-]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_\-]{30,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # key=value only when the value looks like a credential (8+ chars, no spaces or slashes),
    # so "secret: none" or "token: refresh in 1h" stay allowed.
    r"\b(password|passwd)\s*[:=]\s*['\"]?[^\s/'\"]{4,}",
    r"\b(secret|api[_\-]?key|access[_\-]?token|token)\s*[:=]\s*['\"]?[^\s/'\"]{8,}",
    r"\bBearer\s+[A-Za-z0-9._\-]{20,}",
    r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{10,}",  # JWT
    r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@",  # credentials inside a URL
    r"\b[sr]k_(live|test)_[A-Za-z0-9]{16,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
]

_EXFILTRATION = [
    r"\b(curl|wget|iwr|irm)\b[^\n|]*\|\s*(ba|z)?sh\b",
    r"\b(curl|wget)\b[^\n]*\s(-d|--data\S*|--upload-file|-T|-F|--form)(\s|=)",
    r"authorized_keys",
    r"\bnc\s+-e\b",
]

# Zero-width / bidi controls hide text from a human reviewer; other control chars (ESC etc.)
# can repaint a terminal. Newline and tab are allowed.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\x00-\x08\x0b-\x1f\x7f]")

_GROUPS = [
    ("possible prompt injection", [re.compile(p, re.IGNORECASE) for p in _INJECTION]),
    ("looks like a secret or credential", [re.compile(p, re.IGNORECASE) for p in _SECRETS]),
    ("possible exfiltration or backdoor command", [re.compile(p, re.IGNORECASE) for p in _EXFILTRATION]),
]


_SECRET_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _SECRETS]


_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_AWS_SECRET = re.compile(r"\baws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{20,}", re.IGNORECASE)


def redact(text: str) -> str:
    """Replace anything credential-shaped with [REDACTED] (used before archiving transcripts)."""
    text = _PEM_BLOCK.sub("[REDACTED]", text)  # whole key block, not just its header line
    text = _AWS_SECRET.sub("[REDACTED]", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def scan(text: str) -> list[str]:
    """Return the reasons ``text`` is unsafe to store; an empty list means it is clean."""
    reasons = [label for label, patterns in _GROUPS if any(p.search(text) for p in patterns)]
    if _INVISIBLE.search(text):
        reasons.append("contains invisible unicode or control characters")
    return reasons
