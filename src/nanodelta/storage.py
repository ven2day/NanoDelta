"""Small local lake with market/layer/date isolation and idempotent writes."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from nanodelta.contracts import Market


class RecordStore(Protocol):
    def write(
        self,
        *,
        market: Market,
        layer: str,
        event_time: datetime,
        record_id: str,
        record: Mapping[str, Any],
    ) -> bool: ...


class FileLake:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(
        self,
        *,
        market: Market,
        layer: str,
        event_time: datetime,
        record_id: str,
        record: Mapping[str, Any],
    ) -> bool:
        if layer not in {"bronze", "silver", "gold"}:
            raise ValueError(f"unsupported layer: {layer}")
        target = (
            self.root
            / market.value
            / layer
            / f"event_date={event_time.date().isoformat()}"
            / f"{record_id}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        return True
