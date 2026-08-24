from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

SENSITIVE_RE = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|password|cookie|signature|nonce|key)"
    r"([=:]\s*)([^\s,;&]+)"
)


def safe_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[redacted-url]"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact(value: object) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s]+", lambda m: safe_url(m.group(0)) or "", text)
    text = SENSITIVE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    return text
