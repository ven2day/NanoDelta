# NSE symbols and Dhan authentication

This checkpoint turns a user-maintained `symbols.csv` into a validated NSE universe, resolves
missing Dhan security IDs, obtains a Dhan access token, and creates the 730-day history jobs used
by NanoDelta's existing backfill engine. It is market-data plumbing only: it cannot place live
orders.

## 1. Create the symbol universe

Copy the tracked template to the ignored local file:

```powershell
Copy-Item config/nse/symbols.example.csv config/nse/symbols.csv
```

CSV columns:

| Column | Required | Default | Meaning |
|---|---:|---|---|
| `symbol` | yes | — | Canonical uppercase NSE symbol, such as `RELIANCE` |
| `security_id` | no | resolved | Dhan security ID; leave blank for master-file lookup |
| `exchange_segment` | no | `NSE_EQ` | Dhan exchange segment |
| `instrument` | no | `EQUITY` | Dhan instrument type |
| `timeframes` | no | `5m|15m|1h|1d` | Pipe- or semicolon-separated history grains |
| `trade_horizon` | no | `intraday` | Metadata for downstream strategy selection |
| `enabled` | no | `true` | `false`, `0`, `no`, or `n` excludes the row |

Supported values are `1m`, `5m`, `15m`, `30m`, `1h`, `60m`, and `1d`. Duplicate symbols,
unsupported timeframes, an empty enabled universe, and invalid booleans fail validation.

Blank security IDs are matched exactly against Dhan's official detailed security master after
normalizing case and an optional `-EQ` suffix. NanoDelta accepts only NSE equity rows. No match or
more than one security-ID match fails closed, so an ambiguous derivative or similarly named
instrument cannot enter the universe accidentally.

## 2. Store authentication secrets

Dhan calls this value a TOTP secret (RFC 6238). Store the six-digit trading PIN and Base32 TOTP
secret in two files outside source control. Each file must contain only its value and an optional
trailing newline.

Example local layout:

```text
NanoDelta/
└── secrets/                  # ignored by Git
    ├── dhan_pin
    └── dhan_totp_secret
```

Restrict file access to the service account. On Linux, use mode `0600`. On Windows, use NTFS ACLs
so only the NanoDelta service identity and required administrators can read the files. Do not put
either value in `symbols.csv`, a committed `.env` file, logs, screenshots, or support tickets.

## 3. Configure the NSE environment

Copy `env/.env.nse.example` to the secret/environment system used by the deployment. Select one
authentication mode.

Automatic token generation:

```dotenv
DHAN_CLIENT_ID=your-client-id
DHAN_PIN_PATH=C:\NanoDelta\secrets\dhan_pin
DHAN_TOTP_SECRET_PATH=C:\NanoDelta\secrets\dhan_totp_secret
NSE_SYMBOLS_CSV=config/nse/symbols.csv
```

Or use a manually generated token:

```dotenv
DHAN_CLIENT_ID=your-client-id
DHAN_ACCESS_TOKEN=your-current-access-token
NSE_SYMBOLS_CSV=config/nse/symbols.csv
```

When `DHAN_ACCESS_TOKEN` is present it takes precedence. Otherwise both protected-file paths are
required. The automatic provider reads the files only when a token is needed, generates a TOTP,
calls Dhan's token endpoint once, and caches the returned token until five minutes before its
reported expiry. Authentication is done once per universe build, not once per symbol.

## 4. Build and synchronize the universe

Application startup can wire the builder to the existing history engine as follows:

```python
from datetime import UTC, datetime

from nanodelta.contracts import Provider
from nanodelta.history import BackfillEngine
from nanodelta.universe import DhanNseUniverseBuilder

now = datetime.now(UTC)
builder, symbols_path = DhanNseUniverseBuilder.from_environment()
universe = await builder.build(symbols_path, now=now)

engine = BackfillEngine(
    clients={Provider.DHAN: universe.client},
    pipeline=pipeline,
    state=history_state,
    calendars=calendars,
)
runs = await universe.sync_all(engine, now=now, concurrency=4)
```

The surrounding application supplies the existing `pipeline`, durable `history_state`, and
verified market `calendars`. The builder creates one `HistoryJob` for every enabled
symbol/timeframe pair, each targeting 730 days. The history engine then applies pagination,
overlap, settled-boundary checks, watermarks, coverage measurement, and gap repair.

For `30m`, NanoDelta requests Dhan `15m` bars and aggregates only complete pairs anchored at
09:15 IST. A single missing or unsettled 15-minute member suppresses that 30-minute candle so
false completeness is not reported.

## 5. Startup and operating checks

Before enabling the NSE worker, verify:

1. Every enabled CSV symbol resolves to the intended Dhan security ID.
2. The Dhan subscription includes historical data for the requested grains.
3. A verified NSE holiday calendar covers the full 730-day target.
4. The PIN and TOTP files are readable only by the runtime identity.
5. Logs and exception telemetry redact HTTP query parameters and access tokens.
6. History coverage reaches `READY`; a successful HTTP response alone is not readiness.

The resolver downloads Dhan's current detailed instrument master during startup whenever at least
one enabled row has a blank `security_id`. Pinning IDs in the CSV avoids that lookup, but the owner
then assumes responsibility for reviewing instrument changes.

## Official Dhan references

- [Authentication and access-token generation](https://dhanhq.co/docs/v2/authentication/)
- [Instrument list and security-master downloads](https://dhanhq.co/docs/v2/instruments/)
- [Historical data API](https://dhanhq.co/docs/v2/historical-data/)
