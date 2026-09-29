"""Formatting helpers and constants shared across the UI."""
from __future__ import annotations

import html
import re
import time

VR_OFF, VR_FLAT, VR_CROP = 0, 1, 2


def fmt_time(seconds: float | None) -> str:
    if not seconds or seconds != seconds or seconds < 0:
        return "0:00"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(num: float | None) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.2f} TB"


def fmt_hours(seconds: float | None) -> str:
    hours = (seconds or 0) / 3600
    return f"{hours:.0f} h" if hours >= 10 else f"{hours:.1f} h"


def fmt_bitrate(bps: int | None) -> str:
    return f"{bps / 1e6:.1f} Mbps" if bps else ""


def fmt_date(timestamp: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(timestamp)) if timestamp else ""


def res_label(width: int | None, height: int | None, is_vr: bool = False) -> str:
    if not width or not height:
        return ""
    long_side, short_side = max(width, height), min(width, height)
    if is_vr:
        return f"{max(1, round(long_side / 1000))}K"
    if long_side >= 3800 or short_side >= 2000:
        return "4K"
    if long_side >= 2500 or short_side >= 1400:
        return "1440p"
    if long_side >= 1900 or short_side >= 1000:
        return "1080p"
    if long_side >= 1260 or short_side >= 700:
        return "720p"
    return f"{short_side}p"


def breakable_html(text: str) -> str:
    """Escape text for rich-text labels and allow line breaks after path separators."""
    return re.sub(r"([\\/._-])", "\\1\u200b", html.escape(text))
