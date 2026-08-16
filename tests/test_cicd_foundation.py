from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_covers_backend_frontend_compose_and_containers() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    for required in (
        "ruff check .",
        "mypy src",
        "python -m pytest",
        "npm ci --ignore-scripts",
        "npm run lint",
        "npm run build",
        "docker compose --env-file env/.env.production.example config --quiet",
        "docker/build-push-action@v6",
    ):
        assert required in workflow


def test_production_deployment_is_manual_guarded_and_digest_pinned() -> None:
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text()

    assert "workflow_dispatch:" in workflow
    assert "environment:\n      name: production" in workflow
    assert "@sha256:" in workflow
    assert "push:" not in workflow.split("permissions:", maxsplit=1)[0]
    assert "pull_request:" not in workflow


def test_image_publication_records_digest_and_supply_chain_metadata() -> None:
    workflow = (ROOT / ".github/workflows/publish-images.yml").read_text()

    assert "type=sha,format=long" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "steps.build.outputs.digest" in workflow


def test_backup_timer_is_persistent_locked_and_non_overlapping() -> None:
    timer = (ROOT / "deploy/systemd/nanodelta-backup.timer").read_text()
    service = (ROOT / "deploy/systemd/nanodelta-backup.service").read_text()

    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=" in timer
    assert "flock --nonblock" in service
    assert "User=nanodelta" in service
    assert "NoNewPrivileges=true" in service
    assert "ReadWritePaths=/opt/nanodelta/backups /run/lock" in service
