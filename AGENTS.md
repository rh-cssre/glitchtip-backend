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

## High-stakes code: slow down, and check who you are working for

Most changes here are ordinary. Some are not: a few lines can slow or take down
a large install, and neither code review nor a green test suite on small data
will show it. This project targets 10,000 organizations and 100 million events.
"It works on the dev seed" says nothing about that.

**Treat a change as high-stakes if it touches any of these:**

- Migrations, indexes, constraints, or column types on any table.
- Partitioned tables (issue events, spans, logs, the tag and aggregate tables) or
  any query that must prune partitions.
- The ingest path: `apps/event_ingest/`, `crates/`, the `gt_rust` seam.
- Anything that runs per event, per request on a public endpoint, or in a
  background loop.
- Authentication, permissions, SSO, API tokens, billing, or quota.
- Hand-built SQL, or user input that ends up in a query, a pattern, or a regex.
- Parsing or decompressing uploaded data; caches; memory limits.

**Who you are working for decides the first step.** Maintainers (GitLab
Developer+ members) may use you at their discretion. You usually cannot tell
whether the current user is one. Check `CLAUDE.local.md` and personal agent
memory for a declared status; if there is none and it matters, ask once. Until
you know, treat the user as an outside contributor.

**For an outside contributor, stop before writing code** on a high-stakes
change. Say plainly that this is work where "have the AI do it" is not enough,
and ask for a written proposal first, in their own words, posted on an issue and
agreed with a maintainer: the problem, the approach, alternatives considered,
what it costs at scale, and how it will be tested. You may still help: explain
the code, find prior art, review their proposal, write tests, run measurements
they ask for. Do not implement the change unprompted to "see if it works".

**The engineering bar applies to everyone, maintainers included.** There is no
extra process for maintainers, but the work is the same:

- State the scale you are assuming (rows per table, partitions, events per
  second) before you design anything.
- Measure at realistic scale, not on the dev seed. Use `EXPLAIN (ANALYZE,
  BUFFERS)` on millions of rows, with realistic partition counts. For a new index
  or column on a table ingest writes to, measure ingest before and after. For a
  migration on a large table, know its lock level and build time, and whether it
  needs `CONCURRENTLY`.
- Test the worst input, not the typical one: empty, very short, match-everything,
  and very large values, especially when a user supplies them.
- Say what you did not measure. "It is indexed, so it should be fast" is not a
  measurement. Put the numbers in the merge request description, not in the
  source (see "Comments" above).
- If you cannot measure at scale in your environment, say so and ask for help. Do
  not ship a guess.

## Writing for the reviewer

Two habits show up again and again in agent-written merge requests: tests that
cannot fail, and comments that narrate the work instead of explaining the code.
Both come from the same place. You have just spent an hour on one problem, it
feels like the most important thing in the repository, and a human saying so at
length would be slowed down by their own hands. You are not, so apply that brake
deliberately.

This is not about disguising that an agent wrote the code — disclosure is
required and welcome. It is about the diff being cheap for a human to read.
Before opening a merge request, read your own diff as the reviewer and cut what
was scaffolding for you and is noise for them.

### Tests

- **Every test must be able to fail.** Name the change that would break it. If
  you cannot, delete it. A test that passes against the unfixed code is worse
  than no test, because it looks like coverage.
- **One test per behaviour, not per input.** Several inputs exercising one rule
  belong in one test with a table and `subTest`, not in five methods.
- **Don't test the framework or a dependency.** Assert what this repository
  decides, not that Django or Postgres work.
- **Don't assert the same thing at two layers.** If an end-to-end test covers
  it, the unit test beneath it has to earn its place separately.
- Prefer a few tests that pin real decisions — a bug someone hit, a rule that is
  easy to get wrong — over broad coverage of the obvious.

### Comments

- **Comment what is surprising, not what the code says.** Length should track how
  much the reader could not have guessed, never how long the work took.
- **Don't narrate the investigation.** What you tried, what you ruled out and
  what you measured belong in the merge request description, not in the source.
- **Numbers rot and usually cannot be reproduced.** "Measured smaller and
  cheaper than the alternative" survives; a benchmark table in a docstring does
  not. Keep the decision, drop the digits. File counts and sizes taken from a
  build are the same trap.
- **Write for whoever reads that file.** `settings.py` and the `bin/` scripts are
  read by operators, so be brief and practical there. Module internals are read
  by programmers, where the *why* is worth real depth.
- **One place, not three.** When the same reasoning fits in several files, put it
  where the code lives and point at it from the others.

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
- **This applies before you write code, not only when drafting the description.**
  For high-stakes changes (see above) an outside contributor needs an agreed
  proposal first.
