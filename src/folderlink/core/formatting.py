"""画面に出す文字列の整形。取得できなかった値（None）は「—」にする（捏造しない）。"""

from __future__ import annotations

from datetime import datetime

DASH = "—"

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def fmt_bytes(value: int | float | None) -> str:
    if value is None or value < 0:
        return DASH
    size = float(value)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}" if size < 100 else f"{size:.0f} {unit}"
        size /= 1024
    return DASH


def fmt_rate(value: float | None) -> str:
    if value is None or value < 0:
        return DASH
    return fmt_bytes(value) + "/秒"


def fmt_percent(value: float | None) -> str:
    if value is None:
        return DASH
    value = max(0.0, min(100.0, value))
    if value >= 100.0:
        return "100%"
    # 終わっていないものを 100% と表示しない
    return f"{min(int(value), 99)}%"


def fmt_count(value: int | None, unit: str = "件") -> str:
    if value is None or value < 0:
        return DASH
    return f"{value:,} {unit}" if unit else f"{value:,}"


def fmt_time(value: datetime | None, with_date: bool | None = None, now: datetime | None = None) -> str:
    """時刻をこのPCの時間帯で表示。今日なら時刻だけ、それ以外は日付つき。"""
    if value is None:
        return DASH
    local = value.astimezone() if value.tzinfo is not None else value
    today = (now.astimezone() if now is not None and now.tzinfo is not None else (now or datetime.now())).date()
    if with_date is None:
        with_date = local.date() != today
    return local.strftime("%Y/%m/%d %H:%M:%S") if with_date else local.strftime("%H:%M:%S")
