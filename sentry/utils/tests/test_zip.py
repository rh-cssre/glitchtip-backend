import io
import os
import shutil
import tempfile
import warnings
import zipfile

from django.test import SimpleTestCase, override_settings

from sentry.utils.zip import ZipExtractionLimitExceeded, safe_extract_zip

KIB = 1024


def build_zip(entries, compression=zipfile.ZIP_DEFLATED) -> io.BytesIO:
    """Build an in-memory zip from ``(name, content)`` pairs.

    Duplicate names are allowed on purpose; ``zipfile`` only warns.
    """
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w", compression) as zf:
            for name, content in entries:
                zf.writestr(name, content)
    buf.seek(0)
    return buf


def declared_total(buf: io.BytesIO) -> int:
    """Sum of the expanded sizes the archive declares for its members."""
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        total = sum(info.file_size for info in zf.infolist())
    buf.seek(0)
    return total


class SafeExtractZipTests(SimpleTestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest, True)

    def extracted_paths(self):
        found = []
        for root, _dirs, files in os.walk(self.dest):
            for name in files:
                found.append(
                    os.path.relpath(os.path.join(root, name), self.dest).replace(
                        os.sep, "/"
                    )
                )
        return sorted(found)

    def test_normal_bundle_extracts_unchanged(self):
        """A bundle inside the limits still lands on disk byte for byte."""
        buf = build_zip(
            [
                ("manifest.json", b'{"files": {}}'),
                ("files/_/app.js", b"console.log(1)"),
                ("files/_/app.js.map", b'{"version": 3}'),
            ]
        )
        safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(
            self.extracted_paths(),
            ["files/_/app.js", "files/_/app.js.map", "manifest.json"],
        )
        with open(os.path.join(self.dest, "files/_/app.js"), "rb") as f:
            self.assertEqual(f.read(), b"console.log(1)")
        with open(os.path.join(self.dest, "manifest.json"), "rb") as f:
            self.assertEqual(f.read(), b'{"files": {}}')

    def test_strip_toplevel_still_applies(self):
        buf = build_zip([("bundle/a.js", b"a"), ("bundle/b.js", b"b")])
        safe_extract_zip(buf, self.dest)
        self.assertEqual(self.extracted_paths(), ["a.js", "b.js"])

    def test_traversing_members_still_skipped(self):
        """Path traversal handling is unchanged: unsafe members are skipped."""
        buf = build_zip([("../escape.js", b"x"), ("ok.js", b"y")])
        safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(self.extracted_paths(), ["ok.js"])
        self.assertFalse(
            os.path.exists(os.path.join(os.path.dirname(self.dest), "escape.js"))
        )

    def test_rejects_declared_total_over_byte_cap(self):
        """A bomb that declares its size is refused before any bytes are written."""
        buf = build_zip([("bomb.bin", b"\0" * (4 * 1024 * KIB))])
        with (
            override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=64 * KIB),
            self.assertRaises(ZipExtractionLimitExceeded),
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(self.extracted_paths(), [])

    def test_rejects_over_member_cap(self):
        buf = build_zip([(f"f{i}.js", b"x") for i in range(11)])
        with (
            override_settings(GLITCHTIP_MAX_ARCHIVE_MEMBERS=10),
            self.assertRaises(ZipExtractionLimitExceeded) as ctx,
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertIn("entry limit", str(ctx.exception))
        self.assertEqual(self.extracted_paths(), [])

    def test_rejects_when_declared_sizes_understate_bytes_written(self):
        """The cap is spent on bytes written, not on what the archive claims.

        The archive's directory holds eight entries under one name. Extraction
        walks every entry, but each lookup by name resolves to the last one --
        so the 256 KiB member is written eight times while the declared sizes
        account for it once. An implementation that budgets from the declared
        ``ZipInfo.file_size`` values sees an archive comfortably inside the
        limit and lets all 2 MiB through; counting bytes as they are written
        catches it. Removing the write-side counter has to fail this test.
        """
        big = b"B" * (256 * KIB)
        buf = build_zip([("sub/x.js", b"t")] * 7 + [("sub/x.js", big)])

        cap = 640 * KIB
        # The premise: what the archive declares is under the cap, so a
        # declared-size check passes it, while the real output is over.
        self.assertLess(declared_total(buf), cap)
        self.assertGreater(8 * len(big), cap)

        with (
            override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=cap),
            self.assertRaises(ZipExtractionLimitExceeded) as ctx,
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertIn("size limit", str(ctx.exception))
        self.assertEqual(self.extracted_paths(), [])

    def test_partial_output_removed_on_abort(self):
        """Nothing the aborted extraction wrote survives, and nothing else is touched."""
        preexisting = os.path.join(self.dest, "keep.txt")
        with open(preexisting, "wb") as f:
            f.write(b"keep me")

        # Declared sizes stay under the cap so the early reject does not fire
        # and extraction actually gets partway through, writing files and the
        # directories that hold them before it aborts.
        chunk = b"C" * (256 * KIB)
        buf = build_zip(
            [("deep/nested/x.bin", b"t")] * 7 + [("deep/nested/x.bin", chunk)]
        )
        self.assertLess(declared_total(buf), 640 * KIB)

        with (
            override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=640 * KIB),
            self.assertRaises(ZipExtractionLimitExceeded),
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)

        self.assertEqual(self.extracted_paths(), ["keep.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.dest, "deep")))
        with open(preexisting, "rb") as f:
            self.assertEqual(f.read(), b"keep me")

    def test_explicit_limits_override_settings(self):
        buf = build_zip([("a.js", b"a"), ("b.js", b"b")])
        with self.assertRaises(ZipExtractionLimitExceeded):
            safe_extract_zip(buf, self.dest, strip_toplevel=False, max_members=1)
        self.assertEqual(self.extracted_paths(), [])
