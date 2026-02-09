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

## Gotchas
- We optimize postgres column alignment, when making migrations consider column alignment. Some smaller tales don't matter. When in doubt, ask the user.
- Some tables use nested postgres partitions, often organization_id HASH > uuid7 (time). When querying a partitioned tabled, consider optimizing the query to be partition aware
