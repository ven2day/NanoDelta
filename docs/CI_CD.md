# CI/CD contract

NanoDelta separates validation, image publication, and production deployment. An ordinary push can never deploy production.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests and on `main`:

- backend Ruff, strict mypy, and pytest;
- frontend reproducible `npm ci`, lint, and production build;
- Docker Compose rendering and shell syntax checks;
- API and web container builds with BuildKit caching;
- Python and production-frontend dependency audits at high severity.

The committed `web/package-lock.json` is the frontend dependency authority. Update it in the same pull request as `package.json`.

## Immutable image publication

`.github/workflows/publish-images.yml` runs only for a `v*` tag or explicit manual dispatch. It publishes API and web images to GHCR with a full commit-SHA tag, provenance, SBOM, and a digest in the workflow summary. Deploy using the digest reference, never a mutable tag.

## Guarded production deployment

`.github/workflows/deploy-production.yml` is manual-only. Configure the GitHub `production` environment with required reviewers and these secrets:

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_PATH`
- `DEPLOY_SSH_KEY`
- `DEPLOY_KNOWN_HOSTS`

The target directory must already contain `env/.env.production`, secret files described in `secrets/README.md`, and the repository's `scripts/` directory. The workflow accepts only `ghcr.io/...@sha256:...` inputs. It copies the versioned Compose/deploy contract, creates a database backup, pulls images, migrates, starts services, verifies health, and restores the previous application images if a post-backup step fails.

Database migrations remain forward-only. Application rollback does not reverse schema changes. Destructive migrations require an independent restore plan and explicit maintenance window.

The deployment workflow is a contract, not evidence of a successful production deployment. Retain the environment approval, image digests, backup checksum, migration output, and verification log for each release.
