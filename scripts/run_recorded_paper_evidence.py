#!/usr/bin/env python3
"""Generate reproducible, non-live paper-session wiring evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nanodelta.contracts import Market, Provider
from nanodelta.integration import (
    ProviderComposition,
    RecordedHistoricalClient,
    run_recorded_paper_session,
)
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalRequest
from nanodelta.providers.registry import default_provider_registry
from nanodelta.storage import FileLake

ROOT = Path(__file__).resolve().parents[1]


async def generate() -> dict[str, object]:
    fixture = ROOT / "tests/fixtures/providers/dhan_reliance_15m.json"
    now = datetime(2026, 8, 15, 4, 30, tzinfo=UTC)
    with tempfile.TemporaryDirectory(prefix="nanodelta-evidence-") as directory:
        composition = ProviderComposition(
            registry=default_provider_registry(),
            clients={
                Provider.DHAN: RecordedHistoricalClient(Market.NSE, Provider.DHAN, fixture)
            },
            pipeline=EtlPipeline(FileLake(Path(directory))),
        )
        ingestion = await composition.ingest_history(
            Market.NSE,
            HistoricalRequest("RELIANCE", "15m", now - timedelta(hours=1), now, 10),
            received_at=now,
        )
        return run_recorded_paper_session(ingestion).to_dict()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(asyncio.run(generate()), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
