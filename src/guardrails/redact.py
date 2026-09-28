"""Secret redaction and HTML escaping for anything rendered to the UI.

Two distinct hazards are handled here:

1. Subprocess output is rendered verbatim with st.code(). API keys can reach it
   through exception reprs and library debug output.
2. gap_card() interpolates model-generated text into raw HTML rendered with
   unsafe_allow_html=True. A crafted SOP can steer the model into emitting
   markup that then executes in the presenter's browser.
"""
from __future__ import annotations

import html
import os
import re
from typing import Any

from src.guardrails.limits import SECRET_ENV_KEYS

# Ordered: the most specific / highest-confidence patterns run first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-or-v1-[A-Za-z0-9_-]{16,}"), "sk-or-v1-<REDACTED>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),   "sk-ant-<REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"),         "sk-<REDACTED>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer <REDACTED>"),
]

# Keeps the label, masks the value: api_key=abc123 -> api_key=<REDACTED>
_LABELLED = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|auth[_-]?token|token|secret|password|passwd)"
    r"(\s*[:=]\s*)"
    r"[\"']?([^\s\"',}\]]{8,})"
)

# Inline basic-auth credentials in a URL: https://user:pass@host
_URL_AUTH = re.compile(r"(?i)(https?://)([^/\s:@]+):([^/\s@]+)@")


def redact(text: Any) -> str:
    """Strip anything that looks like a credential.

    Total by construction: a failure here returns a placeholder rather than
    risking the raw string reaching the browser.
    """
    try:
        out = text if isinstance(text, str) else str(text)

        # Exact live values first. This catches any key format, including
        # rotated or non-standard ones, and a key split across log formatting.
        for key in SECRET_ENV_KEYS:
            value = os.environ.get(key)
            if value and len(value) > 8:
                out = out.replace(value, f"<{key}_REDACTED>")

        for pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)

        out = _LABELLED.sub(lambda m: f"{m.group(1)}{m.group(2)}<REDACTED>", out)
        out = _URL_AUTH.sub(lambda m: f"{m.group(1)}{m.group(2)}:<REDACTED>@", out)
        return out
    except Exception:
        return "<redaction failed — output suppressed>"


def redact_obj(value: Any) -> Any:
    """Recursively redact every string inside a JSON-shaped structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


def safe_html(text: Any) -> str:
    """Escape untrusted text for interpolation into an unsafe_allow_html block."""
    return html.escape(redact(text), quote=True)


def st_code(container: Any, text: Any, language: str = "text") -> None:
    """container.code() with redaction applied. Use instead of st.code()."""
    container.code(redact(text), language=language)
