from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class DatasetSnapshot:
    snapshot_ref: str
    dataset_id: str
    physical_table: str
    watermark: str
    schema_fingerprint: str
    schema_fields: tuple[str, ...]
    contract_ref: str
    permission_scopes: tuple[str, ...]
    loaded_at: str
    status: str

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetCatalog:
    def __init__(self, snapshots: Iterable[DatasetSnapshot] = ()) -> None:
        self._snapshots = tuple(snapshots)

    def resolve(self, dataset_id: str, *, as_of: datetime, permission_scope: str) -> DatasetSnapshot:
        eligible = [
            candidate
            for candidate in self.as_of_candidates(dataset_id, as_of=as_of)
            if permission_scope in candidate[1].permission_scopes
        ]
        if not eligible:
            raise KeyError(f"dataset_snapshot_unavailable:{dataset_id}")
        return max(eligible, key=lambda candidate: (candidate[0], candidate[1].snapshot_ref))[1]

    def as_of_candidates(
        self,
        dataset_id: str,
        *,
        as_of: datetime,
    ) -> tuple[tuple[datetime, DatasetSnapshot], ...]:
        as_of_utc = _aware_utc(as_of, field="as_of")
        eligible = []
        for item in self._snapshots:
            if item.dataset_id != dataset_id or item.status != "active":
                continue
            loaded_at_utc = _parse_datetime(item.loaded_at)
            if loaded_at_utc <= as_of_utc:
                eligible.append((loaded_at_utc, item))
        return tuple(eligible)

    def common_watermark(self, dataset_ids: tuple[str, ...]) -> date:
        watermarks = []
        for dataset_id in dataset_ids:
            candidates = [
                date.fromisoformat(item.watermark)
                for item in self._snapshots
                if item.dataset_id == dataset_id and item.status == "active"
            ]
            if not candidates:
                raise KeyError(f"dataset_snapshot_unavailable:{dataset_id}")
            watermarks.append(max(candidates))
        return min(watermarks)

    def snapshots(self) -> tuple[DatasetSnapshot, ...]:
        return self._snapshots


def _parse_datetime(value: str) -> datetime:
    return _aware_utc(
        datetime.fromisoformat(value.replace("Z", "+00:00")),
        field="loaded_at",
    )


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"timezone_aware_required:{field}")
    return value.astimezone(timezone.utc)
