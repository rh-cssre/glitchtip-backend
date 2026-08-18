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
  - CI's `lint` job also runs `vulture` (dead-code detection): `vulture apps/ glitchtip/ vulture_whitelist.py --min-confidence 100`. Run it too before pushing — it flags unused code `ruff` won't (e.g. an unused signal-handler parameter); whitelist false positives in `vulture_whitelist.py`.
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
- **Terminology:** Use lowercase `sentry` for the open-source client libraries and their wire formats (e.g. "the MIT sentry SDKs", "a sentry-sdk envelope"). Reserve capital-S **Sentry** for the company, and use it only when the point is genuinely about the company — most often to keep users from confusing GlitchTip with Sentry (e.g. "GlitchTip is not affiliated with Sentry"). There the capital S is doing useful work: it draws the distinction. What to avoid is capital-S Sentry creeping into code comments or docs as a casual synonym for the protocol, the envelope format, or the SDKs.

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
- **Ingest request-body decompression runs in Rust, not Python.** Every ingest endpoint's `Content-Encoding` handling (gzip/deflate/br/zstd) goes through the `gt_rust` extension — `apps/event_ingest/rust_envelope.py` is the seam (`frame_envelope` for `/envelope/`, `decompress_body` for `/store/`, `/security/`, `/minidump/`). It decompresses with a hard, streaming size cap (`GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE`) so a zip-bomb or deeply-recursive payload is bounded before it hits pymalloc. `gt_rust` is therefore a hard ingest dependency (no pure-Python fallback). Don't reintroduce request-body decompression as Django middleware; add new decoders in the Rust core (`crates/gt-envelope`) instead.
- **Artifact bundle extraction has its own caps, separate from the ingest one.** `GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE` bounds ingest bodies only. Uploaded artifact bundles go through `sentry/utils/zip.py`'s `safe_extract_zip`, bounded by `GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE` (bytes written across all members, 512 MB) and `GLITCHTIP_MAX_ARCHIVE_MEMBERS` (entry count, 10,000). Both are also checked against the archive *before* `zipfile.ZipFile()` opens it, because reading a zip's directory allocates an entry object per member — a cap that runs after the open bounds the files written but not the memory spent getting there. Keep new limits on that path pre-open for the same reason.
- **`VTASKS_SCHEDULE` runs once cluster-wide, not per pod.** django-vtasks elects a single scheduler at any moment (the others are idle stand-ins ready to take over). A schedule entry added here fires once per interval across the whole deployment, not once per pod — so you do NOT need an external lock to keep periodic tasks (compaction, retention sweeps, etc.) from running concurrently on every pod. The work itself can assume single-runner semantics. Manual `aenqueue()` calls bypass the scheduler and CAN race with the scheduled run — guard those separately if duplicate execution would matter.
- Minimum Python is 3.12 (`requires-python = ">=3.12"`). Don't add `from __future__ import annotations` — PEP 604 (`X | Y`) and PEP 585 (`list[X]`) are native in 3.10+, and the project does not rely on PEP 563 deferred evaluation.
- **No `a` prefix on async functions in internal code.** GlitchTip is async-first; assume I/O functions are async. Name them `get_foo`, not `aget_foo`. The `a` prefix only makes sense in libraries (Django, allauth, etc.) where a sync sibling exists in the same namespace. Reserve it for code you genuinely intend to upstream into such a project.
- We optimize postgres column alignment, when making migrations consider column alignment. Some smaller tales don't matter. When in doubt, ask the user.
- Some tables use nested postgres partitions, often organization_id HASH > uuid7 (time). When querying a partitioned tabled, consider optimizing the query to be partition aware
- Target scaling up to 10,000 organizations and 100 million events
- **Server-side cursors & PgBouncer:** `DISABLE_SERVER_SIDE_CURSORS=True` is set globally, so `.iterator()` and `.aiterator()` are PgBouncer-safe. In async views prefer `.aiterator()` over `sync_to_async(list)()`. Avoid raw `DECLARE CURSOR` in production code paths.
- **Never create DEFAULT partitions** on partitioned tables. DEFAULT partitions silently absorb rows that miss their intended partition, filling up disk without warning. They also block future partition creation when using nested RANGE->HASH partitioning. It's better to error loudly on a missing partition than to silently fill a default.

## Issues & merge requests — AI policy (for AI coding agents)

This project follows the GlitchTip AI Policy:
https://gitlab.com/glitchtip/glitchtip/-/blob/master/AI_POLICY.md

When you help a user draft an issue or merge request description:

- The **Summary / Description must be the human's own words.** Do not write it
  for them and present it as theirs. Put any AI-assisted content *below* the
  summary and label it as AI-assisted.
- **Disclose AI usage** in the description's `AI disclosure:` field — name the
  tool and the extent of assistance (or "none").
- **Do not submit untested code.** Verify changes with real human use first.
- **Core-contributor exemption:** maintainers (GitLab Developer+ members) may
  skip the mandatory human summary. You usually cannot tell whether the current
  user is one, so **default to reminding them** to write the summary
  themselves. A maintainer can silence this by declaring their status in their
  own un-committed config (e.g. `CLAUDE.local.md` or personal agent memory) —
  check there first.
