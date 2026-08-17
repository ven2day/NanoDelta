"""One-off: resolve a symbol list against Dhan's real instrument master CSV.

Fetches the CSV exactly once (it's large) and resolves every requested symbol
against it in a single pass, reporting resolved and unresolved separately
instead of aborting on the first miss like DhanInstrumentMaster.resolve() does.
"""

import asyncio
import csv
import io
import json
import sys

from nanodelta.universe.nse import DhanInstrumentMaster, HttpxTextTransport

SYMBOLS = [line.strip() for line in sys.stdin if line.strip()]


async def main() -> None:
    master = DhanInstrumentMaster()
    transport = HttpxTextTransport()
    text = await transport.get_text(DhanInstrumentMaster.URL)

    requested = {master._symbol(symbol): symbol for symbol in dict.fromkeys(SYMBOLS)}
    matches: dict[str, dict[str, str]] = {key: {} for key in requested}
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for row in reader:
        if not master._is_nse_equity(row):
            continue
        security_id = master._value(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID")
        if not security_id:
            continue
        aliases = {
            master._symbol(value)
            for value in (
                master._value(row, "SYMBOL_NAME", "SM_SYMBOL_NAME"),
                master._value(row, "TRADING_SYMBOL", "SEM_TRADING_SYMBOL"),
                master._value(row, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"),
                master._value(row, "UNDERLYING_SYMBOL"),
            )
            if value
        }
        for alias in aliases & requested.keys():
            matches[alias][security_id] = security_id

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    ambiguous: list[str] = []
    for normalized, original in requested.items():
        unique = matches[normalized]
        if not unique:
            unresolved.append(original)
        elif len(unique) > 1:
            ambiguous.append(f"{original}: {sorted(unique)}")
        else:
            resolved[original] = next(iter(unique))

    print(json.dumps(resolved, indent=2))
    if unresolved:
        print(f"\n--- {len(unresolved)} UNRESOLVED ---", file=sys.stderr)
        print(", ".join(unresolved), file=sys.stderr)
    if ambiguous:
        print(f"\n--- {len(ambiguous)} AMBIGUOUS ---", file=sys.stderr)
        for line in ambiguous:
            print(line, file=sys.stderr)
    print(f"\nresolved {len(resolved)}/{len(requested)}", file=sys.stderr)


asyncio.run(main())
