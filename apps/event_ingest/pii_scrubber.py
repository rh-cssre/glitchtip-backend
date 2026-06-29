"""Server-side PII / sensitive-data scrubbing for ingested events.

This is the *last line of defense* for sensitive data. Client SDKs can be
configured to scrub data before sending, but the server cannot rely on every
SDK in every language being configured correctly — and an operator running
GlitchTip for other teams often has no control over those SDKs at all. So we
scrub again here, at ingest time, *before* an event is enqueued or persisted.
That ordering matters: the scrubbed payload is what gets serialized into the
task broker (Valkey) and the database, so a redacted value never lands in
either even transiently.

Two independent mechanisms, each toggleable per project:

1. **Key-name matching** — redact the value of any field whose key looks
   sensitive (``password``, ``api_key``, ``Authorization`` header, ...). It
   does not care what the value looks like. The default matcher is
   *token-aware* (split the key on separators and camelCase, match whole
   tokens) so it catches ``auth_token``/``userPassword`` without firing on
   ``author``/``tokenizer``. An opt-in ``aggressive_key_match`` trades that
   precision for substring matching (max recall).

2. **Value-pattern matching** — redact substrings that look sensitive
   regardless of their key (credit-card numbers validated with Luhn, PEM
   private-key blocks, and — opt-in — email addresses). This catches secrets
   that end up in a free-form ``message`` or a stack-frame local.

Design bias: this is a security control aimed at *secrets* (passwords, tokens,
keys, cards), which are always a liability and so are scrubbed by default when
enabled. Broader PII (emails, usernames) is left to opt-in switches, because
many teams intentionally capture it for debugging and it is not the server's
call to discard it. Operators tune the key matcher with ``safe_keys`` (an
allowlist that always wins) and ``sensitive_keys`` (extra keys to redact).

The matcher works on the plain ``dict`` produced by ``model.model_dump()`` — it has
no dependency on the Pydantic schema, so the same engine scrubs errors,
transactions and (eventually) logs. It mutates the dict in place; callers own
that dict (it is freshly built per event), so in-place mutation is both safe
and cheaper than rebuilding it.
"""

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

PLACEHOLDER = "[Filtered]"

# Maximum nesting depth the recursive walk will descend. Event payloads are
# attacker-controlled JSON; this bounds stack use so a pathologically deep
# payload cannot crash ingest. Real events nest only a handful of levels deep.
_MAX_SCRUB_DEPTH = 48

# Default denylist. A wordlist is not itself an implementation — these are the
# field names any error tracker has to treat as sensitive, and they line up
# with what the MIT sentry SDKs scrub client-side so that an event scrubbed
# here looks the same to the frontend as one scrubbed at the source. The
# *matching algorithm* (tokenization, value patterns, section scoping below) is
# an independent implementation.
#
# The default matcher is token-aware (see ``_split_key``): a key is split on
# separators and camelCase boundaries, then each piece is compared. This is
# split into two collections so the matcher gets good recall without the
# false positives a naive substring scan produces:
#
#   _DEFAULT_KEY_TOKENS — sensitive *on their own*, matched against any single
#     token of a key. So ``auth_token``, ``userPassword`` and ``session-id``
#     are caught, but ``author`` (one token, != "auth") and ``tokenizer`` are
#     not.
DEFAULT_KEY_TOKENS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "pin",
        "secret",
        "auth",
        "bearer",
        "credentials",
        "token",
        "session",
        "cookie",
        "csrf",
        "xsrf",
        # PII-flavored additions beyond the SDK default list:
        "ssn",
        "cvv",
        "cvc",
    }
)

#   _DEFAULT_WHOLE_KEYS — multi-word names whose individual tokens are too
#     generic to denylist alone (``key``, ``card``, ``number``...). Matched
#     only against the whole normalized key (separators stripped). So
#     ``api_key``/``apiKey``/``X-Api-Key`` all match, but ``key`` alone and
#     ``card_count`` do not.
DEFAULT_WHOLE_KEYS: frozenset[str] = frozenset(
    {
        "apikey",
        "xapikey",
        "authorization",
        "csrftoken",
        "xcsrftoken",
        "sessionid",
        "setcookie",
        "connectsid",
        "aiohttpsession",
        "mysqlpwd",
        "xforwardedfor",
        "privatekey",
        # PII-flavored additions beyond the SDK default list:
        "creditcard",
        "cardnumber",
        "socialsecurity",
    }
)

# Combined view used only by the opt-in aggressive (substring) matcher.
DEFAULT_DENYLIST: tuple[str, ...] = tuple(DEFAULT_KEY_TOKENS | DEFAULT_WHOLE_KEYS)

# Top-level event sections that may carry user data. Scrubbing is scoped to
# these keys rather than walking the whole event: it keeps structural fields
# (event_id, level, release, environment, platform...) untouched and bounds the
# CPU cost of the walk on the ingest hot path. Within each section the walk is
# fully recursive, so it reaches breadcrumb ``data``, stack-frame ``vars``,
# nested context objects, etc.
EVENT_SECTIONS: tuple[str, ...] = (
    "request",
    "extra",
    "user",
    "contexts",
    "breadcrumbs",
    "exception",
    "threads",
    "tags",
    "logentry",
    "message",
    "csp",  # security/CSP reports (blocked_uri, document_uri, script_sample)
    "spans",  # transaction events
)
# Log items are flat (no sections); they are scrubbed via Scrubber.scrub_log.

# A run of 13–19 digits, optionally separated into groups by spaces or dashes.
# The Luhn check below removes the bulk of false positives (order ids, etc.).
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# PEM-style private key block. Whole value is replaced when this is present.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Separators stripped when normalizing a key for comparison, so that
# ``X-Api-Key``, ``api_key`` and ``apikey`` all collapse to one form.
_KEY_SEPARATORS_RE = re.compile(r"[-_.\s]")

# Split a key into tokens on separators and camelCase boundaries, so
# ``auth_token``, ``authToken`` and ``X-Auth-Token`` all yield ("auth",
# "token"). The lookahead pairs handle aA (camelCase) and the AAa acronym
# boundary (``XMLParser`` -> "XML", "Parser").
_KEY_TOKEN_RE = re.compile(r"[-_.\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _normalize_key(key: str) -> str:
    return _KEY_SEPARATORS_RE.sub("", key).lower()


def _split_key(key: str) -> tuple[str, ...]:
    return tuple(t.lower() for t in _KEY_TOKEN_RE.split(key) if t)


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum, used to keep arbitrary long digit runs from
    being mistaken for card numbers."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class ScrubConfig:
    """Resolved, immutable scrubbing configuration for one project.

    Frozen so it can key the compiled-:class:`Scrubber` cache; tuples (not
    lists) for the same reason.
    """

    enabled: bool = False
    scrub_defaults: bool = True
    sensitive_keys: tuple[str, ...] = ()
    safe_keys: tuple[str, ...] = ()
    # How a key is matched against the denylist:
    #   False (default) — token-aware match. The key is split on separators and
    #     camelCase, so ``auth_token``, ``userPassword`` and ``X-Api-Key`` are
    #     caught, but ``author`` does NOT match ``auth`` and ``tokenizer`` does
    #     NOT match ``token``. Low false-positive rate and ~2-3x cheaper on the
    #     hot path than substring — the low-surprise default.
    #   True — substring match on the normalized key (max recall): anything
    #     containing a denylist token is redacted, including ``author`` and
    #     ``token_count``. Use ``safe_keys`` to claw back specific fields.
    aggressive_key_match: bool = False
    scrub_credit_cards: bool = True
    scrub_private_keys: bool = True
    scrub_emails: bool = False
    placeholder: str = PLACEHOLDER

    @classmethod
    def from_dict(cls, data: dict | str | None) -> "ScrubConfig":
        """Build a config from an untrusted JSON blob (a project's stored
        ``scrub_config``), ignoring unknown keys and coercing types
        defensively — this data is operator-supplied and may be stale.

        Accepts either a dict (the ORM path) or a JSON string (the raw-SQL auth
        path, whose async cursor returns JSONB undecoded)."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return cls()
        if not isinstance(data, dict):
            return cls()

        def _tuple(value) -> tuple[str, ...]:
            if isinstance(value, (list, tuple)):
                return tuple(str(v) for v in value if isinstance(v, (str, int)))
            return ()

        return cls(
            enabled=bool(data.get("enabled", False)),
            scrub_defaults=bool(data.get("scrub_defaults", True)),
            sensitive_keys=_tuple(data.get("sensitive_keys")),
            safe_keys=_tuple(data.get("safe_keys")),
            aggressive_key_match=bool(data.get("aggressive_key_match", False)),
            scrub_credit_cards=bool(data.get("scrub_credit_cards", True)),
            scrub_private_keys=bool(data.get("scrub_private_keys", True)),
            scrub_emails=bool(data.get("scrub_emails", False)),
            placeholder=str(data.get("placeholder", PLACEHOLDER)),
        )


@dataclass
class Scrubber:
    """Compiled scrubber for a single :class:`ScrubConfig`.

    Compiling (normalizing the denylists, deciding which value patterns are
    active) happens once; :meth:`scrub_event` is then called per event.
    """

    config: ScrubConfig
    # Token-aware matcher state (default): single tokens and whole-key forms.
    _key_tokens: frozenset[str] = field(default_factory=frozenset, init=False)
    _whole_keys: frozenset[str] = field(default_factory=frozenset, init=False)
    # Flat denylist used only by the opt-in substring matcher.
    _denylist: frozenset[str] = field(default_factory=frozenset, init=False)
    _safelist: frozenset[str] = field(default_factory=frozenset, init=False)

    def __post_init__(self):
        tokens: set[str] = set()
        whole: set[str] = set()
        if self.config.scrub_defaults:
            tokens.update(DEFAULT_KEY_TOKENS)
            whole.update(DEFAULT_WHOLE_KEYS)
        # Operator-supplied keys are matched as whole normalized keys (the
        # exact field they named) — predictable, and they never inject a broad
        # token that would scrub unrelated fields globally.
        whole.update(_normalize_key(k) for k in self.config.sensitive_keys if k)
        tokens.discard("")
        whole.discard("")
        self._key_tokens = frozenset(tokens)
        self._whole_keys = frozenset(whole)
        # Flat union for the substring fallback.
        self._denylist = frozenset(tokens | whole)
        self._safelist = frozenset(
            _normalize_key(k) for k in self.config.safe_keys if k
        )

    # -- key classification ------------------------------------------------

    def _is_sensitive_key(self, key: str) -> bool:
        nkey = _normalize_key(key)
        if not nkey:
            return False
        if self.config.aggressive_key_match:
            # Substring match (opt-in, max recall): an operator-declared safe
            # token exempts any field containing it.
            if any(safe in nkey for safe in self._safelist):
                return False
            return any(token in nkey for token in self._denylist)
        # Token-aware match (default): a safe key exempts the field, then match
        # the whole normalized key or any single token of it. Catches
        # ``auth_token``/``apiKey`` without firing on ``author``/``tokenizer``.
        if nkey in self._safelist:
            return False
        if nkey in self._whole_keys:
            return True
        # Fast path: a simple lowercase identifier (no separators, no
        # camelCase) is its own single token, so skip the regex split — this
        # covers the bulk of real keys (``username``, ``email``, ``message``).
        # Its single token equals ``nkey``, already cleared against the
        # safelist above.
        if key == nkey:
            return nkey in self._key_tokens
        tokens = _split_key(key)
        # safe_keys is token-aware here too: ``safe_keys=["auth"]`` exempts
        # ``auth_token``, mirroring how the denylist matches tokens.
        if any(token in self._safelist for token in tokens):
            return False
        return any(token in self._key_tokens for token in tokens)

    # -- value scrubbing ---------------------------------------------------

    def _scrub_string(self, value: str) -> str:
        if self.config.scrub_private_keys and "PRIVATE KEY" in value:
            value = _PRIVATE_KEY_RE.sub(self._replace, value)
        if self.config.scrub_credit_cards:
            value = _CARD_RE.sub(self._maybe_redact_card, value)
        if self.config.scrub_emails:
            value = _EMAIL_RE.sub(self._replace, value)
        return value

    def _replace(self, _match: re.Match) -> str:
        # A function replacement (not a string), so an operator-supplied
        # ``placeholder`` such as ``\1`` or ``\g<0>`` is inserted literally
        # rather than interpreted as a regex backreference — which would
        # otherwise raise (``\1``) or re-insert the matched secret verbatim
        # (``\g<0>``) and silently defeat the redaction.
        return self.config.placeholder

    def _maybe_redact_card(self, match: re.Match) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return self.config.placeholder
        return match.group(0)

    def _scrub_node(self, value, depth: int):
        # Depth bound: event payloads are attacker-controlled JSON. The
        # envelope size cap (enforced in Rust) bounds bytes, not nesting depth,
        # so a small-but-deeply-nested payload would otherwise overflow the
        # Python stack (RecursionError -> failed/dropped ingest). Past the
        # bound we stop descending and leave the subtree untouched.
        if depth > _MAX_SCRUB_DEPTH:
            return value
        if isinstance(value, dict):
            return self._scrub_dict(value, depth)
        if isinstance(value, list):
            return self._scrub_list(value, depth)
        if isinstance(value, str):
            return self._scrub_string(value)
        return value

    def _scrub_dict(self, d: dict, depth: int) -> dict:
        for key, value in d.items():
            if isinstance(key, str) and self._is_sensitive_key(key):
                d[key] = self.config.placeholder
            else:
                d[key] = self._scrub_node(value, depth + 1)
        return d

    def _scrub_list(self, lst: list, depth: int) -> list:
        for i, item in enumerate(lst):
            # The ``[[key, value], ...]`` shape GlitchTip stores headers and
            # query strings in (see ``IngestRequest`` in schema.py) needs
            # key-based redaction on element [0]. Detect it per element, so one
            # malformed pair (e.g. a non-string key) does not disable
            # key-redaction for the rest of the list.
            if (
                isinstance(item, (list, tuple))
                and len(item) >= 2
                and isinstance(item[0], str)
            ):
                if self._is_sensitive_key(item[0]):
                    new_value = self.config.placeholder
                else:
                    new_value = self._scrub_node(item[1], depth + 1)
                if isinstance(item, list):
                    item[1] = new_value  # mutate the pair in place
                else:
                    lst[i] = [item[0], new_value, *item[2:]]
            else:
                lst[i] = self._scrub_node(item, depth + 1)
        return lst

    # -- entry point -------------------------------------------------------

    def scrub_event(self, payload: dict) -> dict:
        """Scrub the known PII-bearing sections of an event payload in place."""
        if not self.config.enabled:
            return payload
        for section in EVENT_SECTIONS:
            if section in payload and payload[section] is not None:
                payload[section] = self._scrub_node(payload[section], 0)
        return payload

    def scrub_log(self, log: dict) -> dict:
        """Scrub a flattened log item (``LogItemSchema.model_dump()``) in place.

        Unlike events, logs have no nested sections — the message ``body`` and
        the SDK's promoted ``attributes`` both sit at the top level (see
        ``LogItemSchema.normalize_attributes`` in schema.py) — so the whole
        dict is walked. Structural fields (``level``, ``service``, ``trace_id``
        ...) are not sensitive key names, so they are preserved and only their
        free-text values are pattern-scrubbed."""
        if not self.config.enabled:
            return log
        return self._scrub_dict(log, 0)


_DISABLED_CONFIG = ScrubConfig()


@lru_cache(maxsize=2048)
def _scrubber_for(config: ScrubConfig) -> Scrubber:
    return Scrubber(config)


@lru_cache(maxsize=2048)
def _scrubber_for_json(data: str) -> Scrubber:
    return _scrubber_for(ScrubConfig.from_dict(data))


def get_scrubber(config: ScrubConfig) -> Scrubber:
    """Return a compiled scrubber for a config, reusing the compiled instance
    across events with identical config (the common case — most events for a
    project share one config)."""
    return _scrubber_for(config)


def resolve_scrubber(
    project_config: dict | str | None, default_config: dict | str | None
) -> Scrubber:
    """Resolve the scrubber for a project: its own ``scrub_config`` wins, and a
    project without one falls back to the fleet-wide default. Returns a
    compiled (cached) scrubber. The result may be disabled — callers can still
    call ``scrub_event`` unconditionally, it is a no-op when disabled.

    On the hot path the inputs are usually falsy (no config -> disabled) or a
    JSON string (the raw-SQL auth row returns JSONB undecoded). Both are
    handled without parsing/compiling per event: the disabled case returns a
    shared scrubber, and the JSON-string case is memoized on the raw string so
    ``json.loads`` runs once per distinct config, not once per event."""
    raw = project_config if project_config else default_config
    if not raw:
        return _scrubber_for(_DISABLED_CONFIG)
    if isinstance(raw, str):
        return _scrubber_for_json(raw)
    return _scrubber_for(ScrubConfig.from_dict(raw))
