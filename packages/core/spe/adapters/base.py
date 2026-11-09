from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from spe.licensing import LicenseMetadata, license_for


class AdapterError(RuntimeError):
    pass


@dataclass
class FetchResult:
    records: list[dict[str, Any]]
    observed_at: datetime
    credit_cost: int = 0
    endpoint: str = ""
    raw: Any = None
    warnings: list[str] = field(default_factory=list)


class ProviderAdapter(ABC):
    """Replaceable supplier interface. Models consume canonical records, never raw vendor JSON."""

    provider_id: str

    @property
    def license(self) -> LicenseMetadata:
        return license_for(self.provider_id)

    @abstractmethod
    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_canonical(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return records
