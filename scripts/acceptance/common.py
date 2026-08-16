from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def now() -> datetime:
    return datetime.now(UTC)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603
        args,
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return result.stdout


def command_to_file(args: list[str], path: Path) -> None:
    with path.open("wb") as output:
        subprocess.run(  # noqa: S603
            args, stdout=output, stderr=subprocess.PIPE, check=True
        )


def command_from_file(args: list[str], path: Path) -> None:
    with path.open("rb") as source:
        subprocess.run(  # noqa: S603
            args, stdin=source, capture_output=True, check=True
        )


def fetch(url: str, *, timeout: float = 5, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        return response.read()


def write_evidence(
    path: Path,
    *,
    scenario: str,
    status: str,
    started_at: datetime,
    measurements: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario": scenario,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": now().isoformat(),
        "git_sha": os.environ.get("GITHUB_SHA", "unknown"),
        "environment": os.environ.get("NANODELTA_ACCEPTANCE_ENVIRONMENT", "unknown"),
        "reason": reason,
        "measurements": measurements or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_external_confirmation() -> None:
    if os.environ.get("NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED") != "true":
        raise RuntimeError("NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED=true is required")
