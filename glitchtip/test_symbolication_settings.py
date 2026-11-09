import os
from unittest import mock

from django.test import SimpleTestCase

from glitchtip import settings as settings_module
from glitchtip.settings import _megabytes_setting, _storage_spool_bytes

NAME = "GLITCHTIP_SYMBOLICATION_TEST_MB"


class MegabytesSettingTests(SimpleTestCase):
    def parse(self, raw: str | None):
        environ = {} if raw is None else {NAME: raw}
        with mock.patch.dict(os.environ, environ):
            if raw is None:
                os.environ.pop(NAME, None)
            return _megabytes_setting(NAME, 700)

    def test_whole_numbers_are_used(self):
        for raw, expected in [("0", 0), ("256", 256), (" 64 ", 64)]:
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw), expected)

    def test_unset_or_blank_is_the_default(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw), 700)

    def test_a_bad_value_warns_and_uses_the_default(self):
        for raw in ("1.5", "lots", "-1", "512MB"):
            with (
                self.subTest(raw=raw),
                self.assertWarnsRegex(RuntimeWarning, NAME),
            ):
                self.assertEqual(self.parse(raw), 700)


class StorageSpoolTests(SimpleTestCase):
    def test_spools_past_4_mb_with_a_temporary_directory(self):
        self.assertEqual(_storage_spool_bytes(), 4 * 1024 * 1024)

    def test_keeps_objects_in_memory_without_one(self):
        """A read-only root: spooling would fail every large read."""
        with (
            mock.patch.object(
                settings_module.tempfile,
                "NamedTemporaryFile",
                side_effect=OSError(30, "Read-only file system"),
            ),
            self.assertWarnsRegex(RuntimeWarning, "temporary directory"),
        ):
            self.assertEqual(_storage_spool_bytes(), 0)
