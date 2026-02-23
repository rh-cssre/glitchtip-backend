# AI Agent Guidelines

## Core Workflow
- **Dependency Management:** This project uses `uv`.
- **Database/Services:** Use `docker compose` to run Postgres and Valkey (Redis).
  - Command: `docker compose up -d`
- **Testing:** Always run tests before requesting a code review.
  - Command: `make test` (Runs inside docker)
- **Linting & Formatting:** Adhere to `ruff` standards.
  - Check: `uv run ruff check .`
  - Format: `uv run ruff format .`
- Disclose usage of AI when making a merge request. Human review is REQUIRED for contributions.

## Version Control
- **Commits:** Use conventional commits (e.g., `fix:`, `feat:`, `refactor:`).

## File Structure
- `apps/`: Django apps (feature modules).
- `glitchtip/`: Core project settings and configuration.
- `compose.yml`: Service definitions.

## Licensing — Sentry Code

GlitchTip is API-compatible with Sentry, but **Sentry is NOT open source**. Their server code uses the Business Source License (BSL). You MUST respect this:

- **OFF LIMITS:** Do not read, search, copy, or reference Sentry's server-side source code on GitHub (`getsentry/sentry`, `getsentry/self-hosted`, etc.). This includes browsing their GitHub repos to understand implementation details.
- **ALLOWED:** Reading Sentry's **public documentation** (docs.sentry.io) for API compatibility is fine. We aim to be API-compatible based on their documented public interfaces.
- **ALLOWED:** Reading Sentry's **MIT-licensed SDKs** (e.g., `sentry-python`, `sentry-javascript`, `sentry-ruby`, etc.) is fine — these are genuinely open source.
- **Rule of thumb:** If it's a client SDK (sends data *to* Sentry/GlitchTip), it's MIT and fair game. If it's server-side code (processes/stores data), it's BSL and off limits.

## Local Development

Running `docker compose up` auto-provisions a dev environment with:

- **User:** `test@example.com` / `admin`
- **Organization:** `org`
- **Project:** `project`
- **API Token:** `dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd` (all scopes)
- **DSN:** Printed to stdout on startup (format: `http://<key>@localhost:8000/<project_id>`)

Populate sample data (no arguments needed):
```sh
docker compose exec web python manage.py make_sample_issues
docker compose exec web python manage.py make_sample_logs
docker compose exec web python manage.py make_sample_transactions
```

Test the API:
```sh
curl -H "Authorization: Bearer dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" http://localhost:8000/api/0/organizations/
```

## Gotchas
- We optimize postgres column alignment, when making migrations consider column alignment. Some smaller tales don't matter. When in doubt, ask the user.
- Some tables use nested postgres partitions, often organization_id HASH > uuid7 (time). When querying a partitioned tabled, consider optimizing the query to be partition aware
- Target scaling up to 10,000 organizations and 100 million events
