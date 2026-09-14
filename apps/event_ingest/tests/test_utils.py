from django.test import TestCase

from glitchtip.rust_ingest import _filtered_query_string

from ..utils import remove_bad_chars


class FilteredQueryStringTestCase(TestCase):
    """The ingest error-capture context must not carry a usable DSN key.

    The ingest auth path url-decodes parameter *names* before comparing
    them, so every spelling that authenticates has to be masked — not just
    the literal ``sentry_key=``.
    """

    def test_plain_credentials_masked(self):
        for raw in (b"sentry_key=SECRET", b"glitchtip_key=SECRET"):
            with self.subTest(raw=raw):
                self.assertNotIn("SECRET", _filtered_query_string(raw))

    def test_percent_encoded_credential_name_masked(self):
        """``sentry%5Fkey`` and ``%73entry_key`` both authenticate."""
        for raw in (b"sentry%5Fkey=SECRET", b"%73entry_key=SECRET"):
            with self.subTest(raw=raw):
                self.assertNotIn("SECRET", _filtered_query_string(raw))

    def test_every_occurrence_masked_and_other_params_kept(self):
        out = _filtered_query_string(b"a=1&sentry_key=S1&glitchtip_key=S2&b=2")
        self.assertNotIn("S1", out)
        self.assertNotIn("S2", out)
        self.assertIn("a=1", out)
        self.assertIn("b=2", out)

    def test_empty_and_pairless(self):
        self.assertEqual(_filtered_query_string(b""), "")
        # A non-empty string that yields no pairs is reported as a marker
        # rather than echoed verbatim.
        self.assertEqual(_filtered_query_string(b"&&&"), "[unparseable]")
        # Non-UTF8 bytes parse (as an ordinary name) and must not raise; a
        # credential sitting alongside them is still masked.
        self.assertNotIn(
            "SECRET", _filtered_query_string(b"\xff\xfe=1&sentry_key=SECRET")
        )


class UtilsTestCase(TestCase):
    def test_remove_bad_chars(self):
        self.assertEqual(remove_bad_chars({"\u0000a": " "}), {"a": " "})
        self.assertEqual(remove_bad_chars("\u0000"), "")
        self.assertEqual(remove_bad_chars(["\u0000"]), [""])
        self.assertEqual(
            remove_bad_chars([{"\u0000a": {"\u0000b": "b"}}]), [{"a": {"b": "b"}}]
        )
