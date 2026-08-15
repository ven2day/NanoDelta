from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_common_shell_routes_all_market_workspaces() -> None:
    header = source("web/components/Header.tsx")
    page = source("web/app/page.tsx")

    for market_id in ("all", "nse", "forex", "crypto"):
        assert f'id: "{market_id}"' in header
        assert f'activeMarket === "{market_id}"' in page


def test_shared_lifecycle_has_all_six_data_layers() -> None:
    lifecycle = source("web/components/SixLayerLifecycle.tsx")

    for label in (
        "Raw / Bronze",
        "Canonical / Silver",
        "Feature / Gold",
        "Decision",
        "Execution",
        "Outcome",
    ):
        assert label in lifecycle


def test_each_market_workspace_uses_shared_lifecycle() -> None:
    for workspace in ("NseWorkspace.tsx", "ForexWorkspace.tsx", "CryptoWorkspace.tsx"):
        assert "<SixLayerLifecycle" in source(f"web/components/workspaces/{workspace}")


def test_crypto_workspace_is_explicitly_safe_and_unconfigured() -> None:
    crypto = source("web/components/workspaces/CryptoWorkspace.tsx")

    assert "PROVIDER UNCONFIGURED" in crypto
    assert "EXCHANGE ORDERS: OFF" in crypto
    assert "No live or paper orders are being created" in crypto
