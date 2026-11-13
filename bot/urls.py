from __future__ import annotations

import re
from urllib.parse import urlsplit

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

# Хвостовая пунктуация, которая часто прилипает к ссылке в тексте.
_TRAILING = ".,;:!?)]}>\"'«»…"
# Домены с префиксами (m., vm., vt., www.) обрабатываются через endswith.
_STRIP_PREFIXES = ("www.", "m.", "vm.", "vt.", "mobile.")


def _clean_url(raw: str) -> str:
    url = raw.strip()
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    # Закрывающая скобка могла быть частью ссылки (например, в TikTok-хэштегах) —
    # такое встречается редко, поэтому просто отбрасываем.
    return url


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for prefix in _STRIP_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host


def extract_links(text: str, allowed_hosts: tuple[str, ...]) -> list[str]:
    """Возвращает уникальные ссылки на поддерживаемые платформы в порядке появления."""
    if not text:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for match in URL_RE.finditer(text):
        url = _clean_url(match.group(0))
        if not url:
            continue
        host = host_of(url)
        if not host:
            continue
        if not any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result
