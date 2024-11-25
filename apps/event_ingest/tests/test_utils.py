from django.test import TestCase

from ..utils import remove_bad_chars


class UtilsTestCase(TestCase):
    def test_remove_bad_chars(self):
        self.assertEqual(remove_bad_chars({"\u0000a": " "}), {"a": " "})
        self.assertEqual(remove_bad_chars("\u0000"), "")
        self.assertEqual(remove_bad_chars(["\u0000"]), [""])
        self.assertEqual(
            remove_bad_chars([{"\u0000a": {"\u0000b": "b"}}]), [{"a": {"b": "b"}}]
        )
