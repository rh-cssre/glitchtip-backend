# Server-side PII scrubbing — proof of concept

Status: **proof of concept** (engine + ingest wiring + per-project config).
Reviewed-by-human: pending.

## Problem

Events are arbitrary JSON uploaded by client SDKs. We have no control over what
a customer's app puts in `extra`, stack-frame locals, request headers, query
strings, breadcrumb data or a free-form message. Some of it is sensitive:
passwords, API tokens, session cookies, credit-card numbers, private keys.

SDKs *can* scrub before sending (`before_send`, the SDK `EventScrubber`), but
relying on that is a weak guarantee:

- An operator running GlitchTip for other teams doesn't control their SDKs.
- SDK scrubbing has to be configured, per language, and is easy to get wrong.
- Once unscrubbed PII reaches the server it is already a liability — it lands in
  the task broker, the database, and cold storage.

So we want a **server-side** scrubber: a last line of defense applied at ingest,
before the event is enqueued or persisted.

## What this PoC does

A small, dependency-free engine (`apps/event_ingest/pii_scrubber.py`) plus
wiring into every error/transaction ingest entry point.

### Where it runs (the seam)

Scrubbing happens in the **view**, right after Pydantic validation and *before*
the `IngestTaskMessage` is built and enqueued. That ordering is deliberate: the
scrubbed dict is what gets serialized into Valkey and Postgres, so a redacted
value never lands in the broker or DB even transiently.

Wired sites:

- `views.py` — envelope `event`, envelope `transaction`, the minidump fallback
  inside `event_envelope_view`, and the envelope `log`/`otel_log` items; plus
  the dedicated `minidump_view`.
- `api.py` — legacy `/store/` and `/security/` (CSP) endpoints.
- `otlp_views.py` — native OTLP logs (`POST /v1/logs`).

Logs are flat (no nested sections — the message `body` and the SDK's promoted
`attributes` sit at the top level), so they go through a dedicated
`Scrubber.scrub_log` that walks the whole log dict; structural fields (`level`,
`service`, `trace_id`, ...) are not sensitive key names so they are preserved.

### Two mechanisms

1. **Key-name matching**. Redact the value of any field whose key looks
   sensitive. The default matcher is **token-aware**: a key is split on
   separators *and* camelCase, then matched whole-token, so `auth_token`,
   `userPassword` and `X-Api-Key` are caught while `author` and `tokenizer`
   are left alone. Multi-word forms whose pieces are too generic to denylist
   individually (`api_key` → `apikey`) are matched against the whole
   normalized key. The default wordlist mirrors the MIT sentry SDK denylist
   (so a server-scrubbed event looks the same to the frontend as a
   client-scrubbed one) plus a few PII-flavored additions (`ssn`,
   `card_number`, `cvv`, ...). An opt-in `aggressive_key_match` switches to
   naive substring matching (max recall, more false positives like `author`),
   with `safe_keys` to claw individual fields back.

2. **Value-pattern matching** (catches secrets in free text regardless of key).
   - Credit-card numbers, validated with a Luhn checksum to cut false positives.
   - PEM private-key blocks.
   - Email addresses — **opt-in** (`scrub_emails`), since emails are often
     intentionally captured.

### Section scoping

The walk is scoped to event sections that can carry user data (`request`,
`extra`, `user`, `contexts`, `breadcrumbs`, `exception`/`threads` frame vars,
`tags`, `logentry`, `message`, `csp` reports, transaction `spans`). Within a
section the walk is fully recursive but **depth-bounded** (`_MAX_SCRUB_DEPTH`):
event payloads are attacker-controlled JSON and the envelope size cap (enforced
in Rust) bounds bytes, not nesting depth, so without a bound a small-but-deep
payload could overflow the Python stack and fail ingest. Scoping also keeps
structural fields (`event_id`, `level`, `release`, ...) untouched and bounds CPU
on the hot path.

It also understands GlitchTip's `[[key, value], ...]` storage shape for headers
and query strings (see `IngestRequest` in `schema.py`) and redacts by the pair's
key.

### Configuration

Per-project, with a fleet-wide fallback. Resolution order:

1. `Project.scrub_config` (a JSON object on the project) — if set, wins.
2. `settings.GLITCHTIP_PII_SCRUB_DEFAULT` (env `GLITCHTIP_PII_SCRUB_DEFAULT`,
   a JSON object) — applies to projects with no config of their own.
3. Otherwise scrubbing is **off** (default — no behavior change on upgrade).

`scrub_config` / the default accept:

| key | default | meaning |
|-----|---------|---------|
| `enabled` | `false` | master switch |
| `scrub_defaults` | `true` | apply the built-in key denylist |
| `sensitive_keys` | `[]` | extra whole keys to redact |
| `safe_keys` | `[]` | allowlist tokens/keys that are **never** redacted (wins over denylist; token-aware, so `["auth"]` exempts `auth_token`) |
| `aggressive_key_match` | `false` | naive substring matching instead of token-aware (higher recall, more false positives) |
| `scrub_credit_cards` | `true` | Luhn-validated card redaction |
| `scrub_private_keys` | `true` | PEM private-key redaction |
| `scrub_emails` | `false` | email redaction |
| `placeholder` | `"[Filtered]"` | replacement string |

The project config threads through the ingest auth path: it was added to the
`get_project_auth_info` stored procedure and to `ProjectAuthInfo`
(migration `projects/0023_project_scrub_config`).

## What this looks like to the end user

Most operators who ask for "PII scrubbing" can't actually enumerate what they
want scrubbed — what they want is a **defensible compliance story**: to be able
to tell an auditor "credentials and payment data are stripped server-side before
they are ever stored, regardless of how our app's SDK is configured." So the
feature is framed around two populations:

- **Secrets** (passwords, tokens, API keys, private keys, card numbers). Nobody
  has a legitimate reason to keep these in plaintext in an error tracker;
  storing them *is* the liability. These are safe to scrub aggressively.
- **PII / debugging aids** (emails, usernames, IP addresses, arbitrary `extra`).
  Many teams *intentionally* capture these to debug ("which user hit this?").
  Scrubbing them by default would be surprising data loss, and it is genuinely
  not our call to decide a team shouldn't keep their own users' emails.

The config reflects that split: the secret-oriented mechanisms (key denylist,
cards, private keys) default **on** when scrubbing is enabled; email/PII
scrubbing is opt-in. An operator who "just wants compliance" enables scrubbing
and gets the secrets baseline without having to enumerate anything.

### Not theater

For this to be a real control and not a checkbox, the matching has to actually
catch real-world key names. That is why the default matcher is token-aware
rather than exact: exact-normalized matching (what the SDK does) silently misses
`auth_token`, `user_password` and the like — the single most common shapes —
which would *look* like scrubbing while leaking the majority of cases. Token
matching catches the compounds without the substring matcher's `author`-style
false positives.

## Design decisions / trade-offs

- **Secrets-aggressive, PII-conservative.** The denylist + card + private-key
  mechanisms are on by default (when enabled); email scrubbing is opt-in. This
  is the line between "data that is always a liability" and "data a team may
  legitimately want."

- **Token-aware matching by default, substring opt-in.** Token matching is the
  sweet spot between exact (misses `auth_token`) and substring (redacts
  `author`). `aggressive_key_match` restores substring for operators who prefer
  to over-scrub, with `safe_keys` as the escape hatch.

- **Scrub in the view, not the worker.** Costs request-path CPU but keeps
  unscrubbed PII out of the broker and DB entirely. For a privacy feature that
  ordering is the whole point.

- **Compiled scrubbers are cached** (`lru_cache` keyed on the frozen config), so
  the regex/denylist compilation happens once, not per event. The per-event cost
  is the recursive walk.

- **Pure-dict engine, no schema coupling.** The same engine scrubs errors,
  transactions and logs (errors/transactions via section scoping; logs via
  `scrub_log`, since they are flat), and is trivially unit-testable without a
  DB.

- **Depth-bounded walk.** The recursive walk stops at `_MAX_SCRUB_DEPTH`
  because payloads are attacker JSON; a small-but-deeply-nested payload would
  otherwise overflow the stack and fail ingest (the Rust size cap bounds bytes,
  not depth).

- **Placeholder inserted via a function replacement**, never as a regex
  replacement string, so an operator placeholder like `\g<0>` cannot
  re-insert the matched secret or raise.

## Performance

Measured on a ~8 KB error event (30 stack frames with locals, 20 breadcrumbs,
request headers/query, `extra`), pure-Python engine, indicative laptop numbers:

| matcher | µs / event |
|---------|-----------:|
| disabled (no-op) | ~0.25 |
| token-aware (default) | ~260 |
| aggressive substring | ~620 |

So scrubbing an event costs roughly **0.26 ms** of request-path CPU when
enabled. The walk is section-scoped and the common case (simple lowercase keys)
is fast-pathed to plain set lookups, avoiding the regex split. When scrubbing
is **disabled** (the default), the per-event cost is ~0.25 µs:
`resolve_scrubber` returns a shared disabled scrubber without parsing config,
and JSON-string configs are memoized so `json.loads` runs once per distinct
config, not once per event.

**This is the lever for the default decision.** If scrubbing stays opt-in, the
cost only lands on projects that asked for it and the Python implementation is
fine. If it is ever defaulted on fleet-wide at the 100M-events target, ~0.35 ms
× every event is real CPU, and the natural next step is to move the scrub into
the Rust envelope core (`crates/gt-envelope`) so it happens during framing
rather than as a second Python pass. The config model and wire format are
designed to port cleanly.

## Tests

- `tests/test_pii_scrubber.py` — engine unit tests (token vs substring key
  matching, token-aware safe-keys, Luhn cards, private keys, header/query
  pairs, frame vars, breadcrumbs, log scrubbing, config parsing incl. the
  JSON-string path from the raw-SQL auth row, the depth bound, and the
  placeholder-backreference hardening).
- `tests/test_pii_scrubber_ingest.py` — end-to-end through the real envelope
  endpoint: enabled redacts, disabled passes through, `safe_keys` override.

## Left for production

- **UI** to edit `scrub_config` (currently DB/admin/API only) and ideally
  org-level defaults (add `Organization.scrub_config`, mirror the project
  fallback) so the org overrides the fleet default the way `scrub_ip_addresses`
  already does.
- **`_meta` annotations.** The SDK marks scrubbed fields in a `_meta` block so
  the UI can show "redacted by config". We only substitute the value; consider
  emitting `_meta` for parity if the frontend wants to badge redactions.
- **Decide the default (the one product call).** Three options, in order of
  boldness:
  1. *Off by default* (current). Zero behavior change on upgrade; the auditor
     sees an unused capability unless the operator turns it on. Safe but the
     control mostly goes unused.
  2. *Secrets-on by default* (recommended). Ship `GLITCHTIP_PII_SCRUB_DEFAULT`
     enabling the secrets baseline (denylist + cards + private keys, token
     matching, **no** email/PII). Every install protects credentials out of the
     box — the real compliance win — while emails/usernames are untouched.
     Residual risk: a team intentionally storing their own token in `extra`
     loses it; they opt that project to `off`. Low false-positive rate now that
     the default matcher is token-aware (not substring).
  3. *PII-on by default.* Too aggressive — breaks the debugging-PII population
     and makes the data-loss decision for them.

  Flipping between (1) and (2) is a one-line settings change. My recommendation
  is (2): it's the difference between a feature and a control, and it is the
  thing that actually makes the auditor happy.
- **Visibility / evidence.** An auditor (and a confused developer staring at a
  `[Filtered]` field) both benefit from *proof it ran*: a per-event redaction
  count, a queryable marker, or `_meta`-style annotations the UI can badge.
  Deferred here to avoid coupling to a specific event-protocol annotation
  format; worth doing before this is sold as a compliance feature.
- The hot-path cost is now measured (see Performance); the remaining decision is
  the Rust-port threshold, which only bites if the default goes on fleet-wide.
