from __future__ import annotations

import html


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_caption(
    title: str,
    uploader: str,
    url: str,
    duration: float | None = None,
    height: int | None = None,
) -> str:
    safe_title = html.escape(_shorten(title, 220) or "Видео")
    lines = [f"<b>{safe_title}</b>"]

    if uploader:
        lines.append(f"👤 {html.escape(_shorten(uploader, 80))}")

    details: list[str] = []
    if duration and duration > 0:
        details.append(f"⏱ {format_duration(duration)}")
    if height and height >= 240:
        details.append(f"📺 {height}p")
    if details:
        lines.append(" · ".join(details))

    lines.append(f'🔗 <a href="{html.escape(url, quote=True)}">Оригинал</a>')
    caption = "\n".join(lines)

    # Telegram ограничивает подпись 1024 символами.
    if len(caption) > 1000:
        caption = caption[:997].rstrip() + "…"
    return caption
