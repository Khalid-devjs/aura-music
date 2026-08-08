"""Formatting helpers."""


def format_duration(seconds: int | float) -> str:
    """123 -> '2:03'"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_number(n: int | float) -> str:
    """1234567 -> '1.2M'"""
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def progress_bar(percent: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = round(length * percent / 100)
    bar = "▰" * filled + "▱" * (length - filled)
    return bar


def readable_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def truncate(text, limit: int = 50) -> str:
    if text is None:
        return ""
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quote_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
