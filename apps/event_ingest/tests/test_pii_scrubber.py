from django.test import SimpleTestCase

from ..pii_scrubber import (
    _MAX_SCRUB_DEPTH,
    PLACEHOLDER,
    Scrubber,
    ScrubConfig,
    get_scrubber,
    resolve_scrubber,
)


def scrubber(**overrides) -> Scrubber:
    """A scrubber that is enabled by default, for terse tests."""
    return Scrubber(ScrubConfig(enabled=True, **overrides))


class KeyMatchingTests(SimpleTestCase):
    def test_default_denylist_keys_redacted(self):
        s = scrubber()
        event = {"extra": {"password": "hunter2", "username": "alice"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["password"], PLACEHOLDER)
        self.assertEqual(event["extra"]["username"], "alice")

    def test_key_matching_is_case_and_separator_insensitive(self):
        s = scrubber()
        event = {
            "extra": {
                "API_KEY": "x",
                "api-key": "y",
                "apiKey": "z",
                "X-Api-Key": "w",
            }
        }
        s.scrub_event(event)
        for value in event["extra"].values():
            self.assertEqual(value, PLACEHOLDER)

    def test_token_match_catches_compounds_not_incidental_substrings(self):
        # Default matching is token-aware: split on separators/camelCase and
        # match whole tokens. "auth_token" and "userPassword" are caught;
        # "author" and "tokenizer" are not (they are single non-matching
        # tokens). This is the low-surprise default.
        s = scrubber()
        event = {
            "extra": {
                "auth_token": "x",
                "userPassword": "y",
                "author": "j. doe",
                "tokenizer": "bpe",
            }
        }
        s.scrub_event(event)
        self.assertEqual(event["extra"]["auth_token"], PLACEHOLDER)
        self.assertEqual(event["extra"]["userPassword"], PLACEHOLDER)
        self.assertEqual(event["extra"]["author"], "j. doe")
        self.assertEqual(event["extra"]["tokenizer"], "bpe")

    def test_whole_key_forms_match_but_generic_pieces_do_not(self):
        # "api_key"/"apiKey"/"X-Api-Key" match the whole-key form "apikey",
        # but a bare "key" or "card_count" does not.
        s = scrubber()
        event = {
            "extra": {"X-Api-Key": "x", "apiKey": "y", "key": "ok", "card_count": 3}
        }
        s.scrub_event(event)
        self.assertEqual(event["extra"]["X-Api-Key"], PLACEHOLDER)
        self.assertEqual(event["extra"]["apiKey"], PLACEHOLDER)
        self.assertEqual(event["extra"]["key"], "ok")
        self.assertEqual(event["extra"]["card_count"], 3)

    def test_aggressive_match_catches_incidental_substrings(self):
        s = scrubber(aggressive_key_match=True)
        event = {"extra": {"author": "x", "tokenizer": "y"}}
        s.scrub_event(event)
        # substring mode is intentionally over-eager
        self.assertEqual(event["extra"]["author"], PLACEHOLDER)
        self.assertEqual(event["extra"]["tokenizer"], PLACEHOLDER)

    def test_safe_keys_override_denylist(self):
        s = scrubber(safe_keys=("authorization",))
        event = {"extra": {"authorization": "Bearer x", "password": "y"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["authorization"], "Bearer x")
        self.assertEqual(event["extra"]["password"], PLACEHOLDER)

    def test_custom_sensitive_keys(self):
        s = scrubber(sensitive_keys=("internal_id",))
        event = {"extra": {"internal_id": "secret", "public_id": "ok"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["internal_id"], PLACEHOLDER)
        self.assertEqual(event["extra"]["public_id"], "ok")

    def test_scrub_defaults_disabled(self):
        s = scrubber(scrub_defaults=False, sensitive_keys=("custom",))
        event = {"extra": {"password": "kept", "custom": "gone"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["password"], "kept")
        self.assertEqual(event["extra"]["custom"], PLACEHOLDER)


class SectionScopingTests(SimpleTestCase):
    def test_structural_fields_untouched(self):
        s = scrubber()
        event = {
            "event_id": "abc",
            "level": "error",
            "release": "1.0",
            "extra": {"token": "x"},
        }
        s.scrub_event(event)
        self.assertEqual(event["event_id"], "abc")
        self.assertEqual(event["level"], "error")
        self.assertEqual(event["release"], "1.0")
        self.assertEqual(event["extra"]["token"], PLACEHOLDER)

    def test_top_level_sensitive_key_outside_section_is_ignored(self):
        # "password" as a top-level key is not in a scrubbed section.
        s = scrubber()
        event = {"password": "not-walked", "extra": {"password": "walked"}}
        s.scrub_event(event)
        self.assertEqual(event["password"], "not-walked")
        self.assertEqual(event["extra"]["password"], PLACEHOLDER)

    def test_stack_frame_vars_scrubbed(self):
        s = scrubber()
        event = {
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "stacktrace": {
                            "frames": [
                                {
                                    "function": "login",
                                    "vars": {
                                        "password": "hunter2",
                                        "user": "alice",
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }
        s.scrub_event(event)
        frame = event["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(frame["vars"]["password"], PLACEHOLDER)
        self.assertEqual(frame["vars"]["user"], "alice")
        self.assertEqual(frame["function"], "login")

    def test_breadcrumb_data_scrubbed(self):
        s = scrubber()
        event = {
            "breadcrumbs": {
                "values": [{"message": "logged in", "data": {"auth_token": "abc"}}]
            }
        }
        s.scrub_event(event)
        crumb = event["breadcrumbs"]["values"][0]
        self.assertEqual(crumb["data"]["auth_token"], PLACEHOLDER)
        self.assertEqual(crumb["message"], "logged in")


class KeyValueListTests(SimpleTestCase):
    def test_headers_list_pairs_scrubbed(self):
        s = scrubber()
        event = {
            "request": {
                "headers": [
                    ["Authorization", "Bearer secret"],
                    ["User-Agent", "curl/8"],
                ]
            }
        }
        s.scrub_event(event)
        headers = dict(event["request"]["headers"])
        self.assertEqual(headers["Authorization"], PLACEHOLDER)
        self.assertEqual(headers["User-Agent"], "curl/8")

    def test_query_string_list_pairs_scrubbed(self):
        s = scrubber()
        event = {"request": {"query_string": [["api_key", "abc123"], ["page", "2"]]}}
        s.scrub_event(event)
        qs = dict(event["request"]["query_string"])
        self.assertEqual(qs["api_key"], PLACEHOLDER)
        self.assertEqual(qs["page"], "2")

    def test_pair_shape_preserved(self):
        s = scrubber()
        event = {"request": {"headers": [["X-Page", "1"]]}}
        s.scrub_event(event)
        self.assertEqual(event["request"]["headers"], [["X-Page", "1"]])


class ValuePatternTests(SimpleTestCase):
    def test_credit_card_redacted_with_luhn(self):
        s = scrubber()
        # 4111111111111111 is a valid Luhn test number
        event = {"extra": {"note": "card 4111 1111 1111 1111 on file"}}
        s.scrub_event(event)
        self.assertIn(PLACEHOLDER, event["extra"]["note"])
        self.assertNotIn("4111", event["extra"]["note"])

    def test_non_luhn_digit_run_kept(self):
        s = scrubber()
        # order id, not a card number — fails Luhn
        event = {"extra": {"order": "1234567890123456"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["order"], "1234567890123456")

    def test_private_key_block_redacted(self):
        s = scrubber()
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA\n"
            "-----END RSA PRIVATE KEY-----"
        )
        event = {"extra": {"config": f"key={pem}"}}
        s.scrub_event(event)
        self.assertNotIn("MIIEpAIBAAKCAQEA", event["extra"]["config"])
        self.assertIn(PLACEHOLDER, event["extra"]["config"])

    def test_email_opt_in(self):
        off = scrubber()
        on = scrubber(scrub_emails=True)
        e1 = {"extra": {"msg": "contact alice@example.com"}}
        e2 = {"extra": {"msg": "contact alice@example.com"}}
        off.scrub_event(e1)
        on.scrub_event(e2)
        self.assertIn("alice@example.com", e1["extra"]["msg"])
        self.assertNotIn("alice@example.com", e2["extra"]["msg"])

    def test_card_in_message_section(self):
        s = scrubber()
        event = {"message": "failed for 4111111111111111"}
        s.scrub_event(event)
        self.assertNotIn("4111111111111111", event["message"])


class ConfigTests(SimpleTestCase):
    def test_disabled_is_noop(self):
        s = Scrubber(ScrubConfig(enabled=False))
        event = {"extra": {"password": "hunter2"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["password"], "hunter2")

    def test_from_dict_coerces_and_ignores_unknown(self):
        config = ScrubConfig.from_dict(
            {
                "enabled": True,
                "sensitive_keys": ["foo", 5, None],
                "safe_keys": "notalist",
                "bogus": "ignored",
            }
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.sensitive_keys, ("foo", "5"))
        self.assertEqual(config.safe_keys, ())

    def test_from_dict_none_is_disabled_default(self):
        self.assertEqual(ScrubConfig.from_dict(None), ScrubConfig())

    def test_from_dict_accepts_json_string(self):
        # The raw-SQL auth path returns JSONB undecoded (a string).
        config = ScrubConfig.from_dict('{"enabled": true, "safe_keys": ["x"]}')
        self.assertTrue(config.enabled)
        self.assertEqual(config.safe_keys, ("x",))

    def test_from_dict_invalid_json_string_is_disabled(self):
        self.assertEqual(ScrubConfig.from_dict("not json"), ScrubConfig())

    def test_get_scrubber_caches_by_config(self):
        config = ScrubConfig(enabled=True)
        self.assertIs(get_scrubber(config), get_scrubber(config))

    def test_custom_placeholder(self):
        s = scrubber(placeholder="***")
        event = {"extra": {"password": "x"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["password"], "***")


class HardeningTests(SimpleTestCase):
    def test_deeply_nested_payload_does_not_recurse_without_bound(self):
        # Attacker-controlled JSON can be small in bytes but very deep. A chain
        # this deep would overflow the Python stack (RecursionError -> failed
        # ingest) without the depth bound; with it, the walk stops descending.
        s = scrubber()
        node: dict = {"leaf": "x"}
        for _ in range(3000):
            node = {"child": node}
        # Must not raise RecursionError.
        s.scrub_event({"extra": node})
        # Shallow secrets (within the bound) are still scrubbed.
        shallow = {"extra": {"a": {"b": {"password": "x"}}}}
        s.scrub_event(shallow)
        self.assertEqual(shallow["extra"]["a"]["b"]["password"], PLACEHOLDER)
        self.assertGreater(_MAX_SCRUB_DEPTH, 0)

    def test_placeholder_with_regex_backreference_is_literal(self):
        # An operator-supplied placeholder containing \g<0> or \1 must be
        # inserted literally, never interpreted as a regex backreference (which
        # would re-insert the matched secret or raise).
        for ph in ("\\g<0>", "\\1"):
            s = scrubber(placeholder=ph, scrub_emails=True)
            pem = "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----"
            event = {"extra": {"a": "contact x@y.com", "b": pem}}
            s.scrub_event(event)
            self.assertEqual(event["extra"]["a"], "contact " + ph)
            self.assertNotIn("SECRET", event["extra"]["b"])
            self.assertEqual(event["extra"]["b"], ph)

    def test_safe_keys_are_token_aware_in_default_mode(self):
        # safe_keys=["session"] must exempt "session_token" (a per-token match),
        # not only an exact "session" key.
        s = scrubber(safe_keys=("session",))
        event = {"extra": {"session_token": "keep", "password": "scrub"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["session_token"], "keep")
        self.assertEqual(event["extra"]["password"], PLACEHOLDER)

    def test_mixed_key_value_list_still_redacts_valid_pairs(self):
        # One malformed pair (non-string key) must not disable key-redaction
        # for the rest of the list.
        s = scrubber()
        event = {
            "request": {
                "headers": [
                    [None, "weird"],
                    ["Authorization", "Bearer secret"],
                    ["Accept", "json"],
                ]
            }
        }
        s.scrub_event(event)
        headers = event["request"]["headers"]
        self.assertEqual(headers[1], ["Authorization", PLACEHOLDER])
        self.assertEqual(headers[2], ["Accept", "json"])

    def test_additional_secret_tokens(self):
        s = scrubber()
        event = {"extra": {"passphrase": "a", "pin": "b", "bearer": "c"}}
        s.scrub_event(event)
        self.assertEqual(event["extra"]["passphrase"], PLACEHOLDER)
        self.assertEqual(event["extra"]["pin"], PLACEHOLDER)
        self.assertEqual(event["extra"]["bearer"], PLACEHOLDER)


class LogScrubbingTests(SimpleTestCase):
    def test_scrub_log_redacts_flat_body_and_attributes(self):
        s = scrubber()
        # LogItemSchema.dict() is flat: body + promoted attribute extras +
        # structural fields at the top level.
        log = {
            "level": "error",
            "body": "card 4111 1111 1111 1111 declined",
            "service": "api",
            "trace_id": "abc123",
            "db_password": "hunter2",
            "user_id": "42",
        }
        s.scrub_log(log)
        self.assertNotIn("4111", log["body"])
        self.assertEqual(log["db_password"], PLACEHOLDER)
        # structural fields preserved
        self.assertEqual(log["level"], "error")
        self.assertEqual(log["service"], "api")
        self.assertEqual(log["trace_id"], "abc123")
        self.assertEqual(log["user_id"], "42")

    def test_scrub_log_disabled_is_noop(self):
        s = Scrubber(ScrubConfig(enabled=False))
        log = {"body": "x", "password": "y"}
        s.scrub_log(log)
        self.assertEqual(log["password"], "y")


class ResolveScrubberTests(SimpleTestCase):
    def test_falsy_configs_resolve_to_disabled(self):
        self.assertFalse(resolve_scrubber(None, None).config.enabled)
        self.assertFalse(resolve_scrubber({}, {}).config.enabled)

    def test_project_config_wins_over_default(self):
        s = resolve_scrubber({"enabled": True}, {"enabled": False})
        self.assertTrue(s.config.enabled)

    def test_json_string_config_is_parsed_and_cached(self):
        # The raw-SQL auth path delivers scrub_config as a JSON string.
        raw = '{"enabled": true}'
        s1 = resolve_scrubber(raw, None)
        s2 = resolve_scrubber(raw, None)
        self.assertTrue(s1.config.enabled)
        self.assertIs(s1, s2)
