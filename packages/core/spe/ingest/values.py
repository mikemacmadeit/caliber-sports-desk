from __future__ import annotations

from typing import Any


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    text = str(value).strip()
    return text in {"", "nan", "NaT", "None", "<NA>"}


def as_str(value: Any) -> str | None:
    if is_missing(value):
        return None
    return str(value).strip()


def as_int(value: Any, default: int | None = 0) -> int | None:
    if is_missing(value):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: Any) -> float | None:
    if is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
