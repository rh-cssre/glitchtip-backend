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
- **Merge requests:** GlitchTip is open source. Write MR titles and descriptions for an outside contributor reading them cold — describe the problem in terms anyone can verify from the code, not from private infra. Don't paste internal issue IDs, staging/prod URLs, API tokens, or snapshots from a personal install; if a bug was found via an error tracker, describe the symptom and reproduction, not the dashboard link.

## File Structure
- `apps/`: Django apps (feature modules).
- `glitchtip/`: Core project settings and configuration.
- `compose.yml`: Service definitions.
- `docs/ai/staging_qa_plan.md`: Reusable QA prompt for staging. Includes workflow, heuristics, testing checklist, and known API differences.

## Licensing — Clean Room Development

GlitchTip is API-compatible with Sentry, but **their server code and documentation are NOT open source**. They use the Business Source License (BSL) and Functional Source License (FSL). You MUST strictly adhere to a clean room development process.

- **OFF LIMITS (Server Code & Docs):** You are strictly prohibited from reading, searching, curling, copying, or referencing Sentry's server-side source code or official documentation. 
  - Do not access `getsentry/sentry`, `getsentry/self-hosted`, or similar server repositories.
  - Do not access `docs.sentry.io` or the `getsentry/sentry-docs` repository. 
- **ALLOWED (MIT SDKs):** You may read and analyze Sentry's **MIT-licensed client SDKs** (e.g., `sentry-python`, `sentry-javascript`). These send data *to* the server and are genuinely open source.
- **How to build for compatibility:** To understand API payloads or endpoints, do not look up their documentation. Instead, inspect the source code of the MIT-licensed SDKs to see what they transmit, or ask the user to provide a captured JSON payload from an SDK to use as a test fixture.
- **Terminology:** Avoid mentioning the company Sentry (capital S) unless explicitly necessary, to prevent trademark confusion. When referencing the client-side libraries, refer to them as "MIT sentry SDKs" (lowercase s).

## Local Development

Running `docker compose up` auto-provisions a dev environment with:

- **User:** `test@example.com` / `admin_pass`
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
- Minimum Python is 3.12 (`requires-python = ">=3.12"`). Don't add `from __future__ import annotations` — PEP 604 (`X | Y`) and PEP 585 (`list[X]`) are native in 3.10+, and the project does not rely on PEP 563 deferred evaluation.
- **No `a` prefix on async functions in internal code.** GlitchTip is async-first; assume I/O functions are async. Name them `get_foo`, not `aget_foo`. The `a` prefix only makes sense in libraries (Django, allauth, etc.) where a sync sibling exists in the same namespace. Reserve it for code you genuinely intend to upstream into such a project.
- We optimize postgres column alignment, when making migrations consider column alignment. Some smaller tales don't matter. When in doubt, ask the user.
- Some tables use nested postgres partitions, often organization_id HASH > uuid7 (time). When querying a partitioned tabled, consider optimizing the query to be partition aware
- Target scaling up to 10,000 organizations and 100 million events
- **Server-side cursors & PgBouncer:** `DISABLE_SERVER_SIDE_CURSORS=True` is set globally, so `.iterator()` and `.aiterator()` are PgBouncer-safe. In async views prefer `.aiterator()` over `sync_to_async(list)()`. Avoid raw `DECLARE CURSOR` in production code paths.
- **Never create DEFAULT partitions** on partitioned tables. DEFAULT partitions silently absorb rows that miss their intended partition, filling up disk without warning. They also block future partition creation when using nested RANGE->HASH partitioning. It's better to error loudly on a missing partition than to silently fill a default.
