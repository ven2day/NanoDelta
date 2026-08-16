from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_ui_has_no_representative_trading_fixtures() -> None:
    page = (ROOT / "web/app/page.tsx").read_text(encoding="utf-8")
    for fabricated in ("SBIN", "₹20,18,420", "signals=["):
        assert fabricated not in page
    assert "Authoritative NanoDelta backend data only" in page


def test_bff_keeps_backend_key_server_side_and_allowlists_reads() -> None:
    proxy = (ROOT / "web/app/api/backend/[...path]/route.ts").read_text(encoding="utf-8")
    assert 'headers: { "X-API-Key": backendApiKey(session.role)' in proxy
    assert "const ALLOWED" in proxy
    assert "export async function GET" in proxy
    assert "export async function POST" not in proxy


def test_session_cookie_security_contract() -> None:
    login = (ROOT / "web/app/api/auth/login/route.ts").read_text(encoding="utf-8")
    assert "httpOnly: true" in login
    assert 'sameSite: "strict"' in login
    assert 'process.env.NODE_ENV === "production"' in login
    auth = (ROOT / "web/lib/auth.ts").read_text(encoding="utf-8")
    assert 'createHmac("sha256"' in auth
    assert "scryptSync" in auth
    assert "timingSafeEqual" in auth


def test_compose_mounts_ui_secrets_read_only() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for secret in ("ui_users.json", "ui_session_key", "ui_backend_keys.json"):
        assert f"./secrets/{secret}" in compose
        assert f"/run/secrets/{secret}:ro" in compose
