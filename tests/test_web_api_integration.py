from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_web_ui_contains_no_representative_trading_records() -> None:
    page = (ROOT / "web/app/page.tsx").read_text(encoding="utf-8")
    forbidden = (
        "SBIN",
        "RELIANCE",
        "₹20,18,420",
        "VWAP Pullback",
        "ALL SYSTEMS NORMAL",
        "representative",
    )
    assert all(value not in page for value in forbidden)
    assert 'fetch(`/api/backend/${path}`' in page
    assert 'backend<ApiPage>("nse/decision-events?limit=500")' in page
    assert "Decision Lifecycle" in page
    assert "Decision Attribution" in page
    assert "Observed Universe" in page
    assert "No authoritative candidates match these filters" in page


def test_bff_is_session_guarded_get_only_and_allowlisted() -> None:
    proxy = (ROOT / "web/app/api/backend/[...path]/route.ts").read_text(encoding="utf-8")
    backend = (ROOT / "web/lib/backend.ts").read_text(encoding="utf-8")
    session = (ROOT / "web/lib/session.ts").read_text(encoding="utf-8")

    assert "validateSession" in proxy
    assert "export async function GET" in proxy
    assert "export async function POST" not in proxy
    assert "allowlistedBackendPath" in proxy
    assert 'method: "GET"' in backend
    assert 'cache: "no-store"' in backend
    assert '"X-API-Key": await apiKey(role)' in backend
    assert "session.role" in proxy
    assert 'httpOnly: true' in session
    assert 'sameSite: "strict"' in session
    assert 'Authorization: `Bearer ${token}`' in session


def test_compose_mounts_web_secrets_without_browser_exposure() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "NANODELTA_BACKEND_URL: http://api:8000" in compose
    assert "NANODELTA_BACKEND_KEYS_PATH: /run/secrets/backend_keys.json" in compose
    assert "./secrets/backend_keys.json:/run/secrets/backend_keys.json:ro" in compose
    assert "NANODELTA_WEB_PASSWORD_FILE" not in compose
    assert "NANODELTA_WEB_SESSION_SECRET_FILE" not in compose
