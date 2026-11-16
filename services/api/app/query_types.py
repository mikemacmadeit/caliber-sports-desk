"""Validated query values that keep the string interface used by the read models."""
from datetime import date
from typing import Annotated, Literal
from pydantic import AfterValidator


def iso_day(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed.year == 9999:
        raise ValueError("Use a calendar date in YYYY-MM-DD format")
    return value


def iso_month(value: str) -> str:
    iso_day(value + "-01")
    return value


IsoDay = Annotated[str, AfterValidator(iso_day)]
IsoMonth = Annotated[str, AfterValidator(iso_month)]
League = Literal["all", "nfl", "fbs"]
