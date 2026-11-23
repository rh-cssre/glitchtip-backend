import io
import os
import shutil
import struct
import tempfile
import warnings
import zipfile
from unittest import mock

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


class _RefusedZipFile:
    """Stand-in for ``zipfile.ZipFile`` that fails if it is ever constructed."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "zipfile.ZipFile was constructed: the archive's directory was read "
            "into memory before the limits could refuse it"
        )


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

    def test_accepts_deep_paths_within_directory_budget(self):
        """Long member paths are not what the pre-open directory check is for."""
        deep = "/".join(["a-fairly-long-directory-name"] * 6)
        buf = build_zip([(f"{deep}/app-{i}.js.map", b"m") for i in range(10)])
        with override_settings(GLITCHTIP_MAX_ARCHIVE_MEMBERS=10):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(len(self.extracted_paths()), 10)

    def test_extracts_into_a_destination_that_does_not_exist_yet(self):
        buf = build_zip([("a.js", b"a")])
        dest = os.path.join(self.dest, "fresh")
        safe_extract_zip(buf, dest, strip_toplevel=False)
        self.assertEqual(os.listdir(dest), ["a.js"])

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

    def test_declared_total_ignores_members_that_are_never_written(self):
        """Skipped entries do not spend the byte budget.

        Only ``ok.js`` reaches the disk; the traversing member and the
        directory entry are skipped. Charging the cap for them refuses an
        archive whose real output is one byte.
        """
        buf = build_zip(
            [
                ("dir/", b""),
                ("../escape.bin", b"\0" * (4 * KIB)),
                ("ok.js", b"y"),
            ]
        )
        with override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=KIB):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(self.extracted_paths(), ["ok.js"])

    def test_declared_total_ignores_members_outside_the_stripped_prefix(self):
        """A dot-prefixed sibling is outside the prefix, so it is not charged."""
        buf = build_zip(
            [
                ("bundle/ok.js", b"y"),
                (".hidden/huge.bin", b"\0" * (4 * KIB)),
            ]
        )
        with override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=KIB):
            safe_extract_zip(buf, self.dest)
        self.assertEqual(self.extracted_paths(), ["ok.js"])

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

    def test_accepts_exactly_the_member_cap(self):
        """The cap is a ceiling, not a strict bound: N entries is still fine."""
        buf = build_zip([(f"f{i}.js", b"x") for i in range(10)])
        with override_settings(GLITCHTIP_MAX_ARCHIVE_MEMBERS=10):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(len(self.extracted_paths()), 10)

    def test_accepts_exactly_the_byte_cap(self):
        """An archive that expands to exactly the cap is written, not refused.

        Hits both byte checks at once: the declared total equals the cap, and
        so does the running count of bytes written.
        """
        cap = 4 * KIB
        buf = build_zip([("exact.bin", b"E" * cap)])
        with override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=cap):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(self.extracted_paths(), ["exact.bin"])
        with open(os.path.join(self.dest, "exact.bin"), "rb") as f:
            self.assertEqual(len(f.read()), cap)

    def test_accepts_an_archive_of_exactly_the_byte_cap_before_expansion(self):
        """The pre-open size check is a ceiling too."""
        buf = build_zip([("a.js", b"a")])
        cap = len(buf.getvalue())
        with override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=cap):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertEqual(self.extracted_paths(), ["a.js"])

    def test_rejects_archive_larger_than_the_byte_cap_before_opening_it(self):
        """An archive bigger than the cap cannot expand to under it.

        ``zipfile.ZipFile`` must not be reached: reading the directory of an
        arbitrarily large archive is itself the allocation being bounded.
        """
        buf = build_zip([("a.js", b"a" * (8 * KIB))], zipfile.ZIP_STORED)
        with (
            override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=KIB),
            mock.patch.object(zipfile, "ZipFile", _RefusedZipFile),
            self.assertRaises(ZipExtractionLimitExceeded) as ctx,
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertIn("before expansion", str(ctx.exception))
        self.assertEqual(self.extracted_paths(), [])

    def test_rejects_oversized_directory_before_opening_the_archive(self):
        """Many empty entries are refused without building a ZipInfo for each.

        The member cap alone cannot do this. ``zipfile.ZipFile()`` reads the
        whole central directory and builds an entry object per member -- about
        540 bytes of resident memory against 88 bytes of archive -- as part of
        construction, so a cap that runs after the open has already let the
        allocation happen. Only the archive's own declared directory size,
        read before the open, bounds it.
        """
        buf = build_zip([(f"f{i}", b"") for i in range(2000)], zipfile.ZIP_STORED)
        with (
            override_settings(GLITCHTIP_MAX_ARCHIVE_MEMBERS=10),
            mock.patch.object(zipfile, "ZipFile", _RefusedZipFile),
            self.assertRaises(ZipExtractionLimitExceeded) as ctx,
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)
        self.assertIn("archive directory", str(ctx.exception))
        self.assertEqual(self.extracted_paths(), [])

    def test_rejects_oversized_directory_declared_only_by_a_zip64_record(self):
        """A small classic record cannot hide a large Zip64 one.

        ``zipfile`` prefers the Zip64 records whenever they are present, not
        only when the classic fields overflow, so an archive can declare a
        harmless directory size in the record a naive check reads while
        ``zipfile`` goes on to walk the size in the Zip64 record instead.
        Both records have to be resolved the way ``zipfile`` resolves them.
        """
        raw = build_zip([(f"f{i}", b"") for i in range(2000)], zipfile.ZIP_STORED)
        data = bytearray(raw.getvalue())
        eocd = data.rfind(b"PK\x05\x06")
        real_directory_size = struct.unpack_from("<I", data, eocd + 12)[0]
        self.assertGreater(real_directory_size, 10 * 512)

        # The classic record now claims one entry's worth of directory.
        struct.pack_into("<I", data, eocd + 12, 46)
        zip64_record = struct.pack(
            "<4sQ2H2L4Q",
            b"PK\x06\x06",
            44,
            45,
            45,
            0,
            0,
            2000,
            2000,
            real_directory_size,
            struct.unpack_from("<I", data, eocd + 16)[0],
        )
        zip64_locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, eocd, 1)
        forged = io.BytesIO(
            bytes(data[:eocd]) + zip64_record + zip64_locator + bytes(data[eocd:])
        )

        with (
            override_settings(GLITCHTIP_MAX_ARCHIVE_MEMBERS=10),
            mock.patch.object(zipfile, "ZipFile", _RefusedZipFile),
            self.assertRaises(ZipExtractionLimitExceeded) as ctx,
        ):
            safe_extract_zip(forged, self.dest, strip_toplevel=False)
        self.assertIn(str(real_directory_size), str(ctx.exception))

    def test_rejects_oversized_directory_given_a_filename(self):
        """The pre-open checks apply to the filename form of the argument too."""
        buf = build_zip([(f"f{i}", b"") for i in range(2000)], zipfile.ZIP_STORED)
        archive_path = os.path.join(self.dest, "many.zip")
        with open(archive_path, "wb") as f:
            f.write(buf.getvalue())
        with (
            override_settings(GLITCHTIP_MAX_ARCHIVE_MEMBERS=10),
            mock.patch.object(zipfile, "ZipFile", _RefusedZipFile),
            self.assertRaises(ZipExtractionLimitExceeded),
        ):
            safe_extract_zip(archive_path, self.dest, strip_toplevel=False)

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
        """Nothing the aborted extraction created survives, and nothing else goes.

        ``keep.txt`` is not in the archive and must come through untouched.
        ``shared.js`` is: extraction overwrites it, which is the archive's
        doing, but cleanup must not go on to delete a file the destination
        already held.
        """
        preexisting = os.path.join(self.dest, "keep.txt")
        with open(preexisting, "wb") as f:
            f.write(b"keep me")
        overwritten = os.path.join(self.dest, "shared.js")
        with open(overwritten, "wb") as f:
            f.write(b"original")

        # Declared sizes stay under the cap so the early reject does not fire
        # and extraction actually gets partway through, writing files and the
        # directories that hold them before it aborts.
        chunk = b"C" * (256 * KIB)
        buf = build_zip(
            [("shared.js", b"replacement")]
            + [("deep/nested/x.bin", b"t")] * 7
            + [("deep/nested/x.bin", chunk)]
        )
        self.assertLess(declared_total(buf), 640 * KIB)

        with (
            override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=640 * KIB),
            self.assertRaises(ZipExtractionLimitExceeded),
        ):
            safe_extract_zip(buf, self.dest, strip_toplevel=False)

        self.assertEqual(self.extracted_paths(), ["keep.txt", "shared.js"])
        self.assertFalse(os.path.exists(os.path.join(self.dest, "deep")))
        with open(preexisting, "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        with open(overwritten, "rb") as f:
            self.assertEqual(f.read(), b"replacement")

    def test_abort_leaves_the_destination_directory_in_place(self):
        """Cleanup takes back what it created, not the directory it was given."""
        dest = os.path.join(self.dest, "scratch")
        chunk = b"C" * (256 * KIB)
        buf = build_zip([("x.bin", b"t")] * 7 + [("x.bin", chunk)])
        with (
            override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=640 * KIB),
            self.assertRaises(ZipExtractionLimitExceeded),
        ):
            safe_extract_zip(buf, dest, strip_toplevel=False)
        self.assertTrue(os.path.isdir(dest))
        self.assertEqual(os.listdir(dest), [])

    def test_explicit_limits_override_settings(self):
        buf = build_zip([("a.js", b"a"), ("b.js", b"b")])
        with self.assertRaises(ZipExtractionLimitExceeded) as ctx:
            safe_extract_zip(buf, self.dest, strip_toplevel=False, max_members=1)
        self.assertIn("entry limit", str(ctx.exception))
        self.assertEqual(self.extracted_paths(), [])

        buf = build_zip([("a.js", b"a" * (4 * KIB))])
        with self.assertRaises(ZipExtractionLimitExceeded) as ctx:
            safe_extract_zip(buf, self.dest, strip_toplevel=False, max_total_bytes=KIB)
        self.assertIn("size limit", str(ctx.exception))
        self.assertEqual(self.extracted_paths(), [])

    def test_explicit_limits_win_over_a_stricter_setting(self):
        """The arguments are the limits in force, not a second ceiling."""
        buf = build_zip([("a.js", b"a" * (4 * KIB)), ("b.js", b"b")])
        with override_settings(
            GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=KIB, GLITCHTIP_MAX_ARCHIVE_MEMBERS=1
        ):
            safe_extract_zip(
                buf,
                self.dest,
                strip_toplevel=False,
                max_total_bytes=64 * KIB,
                max_members=2,
            )
        self.assertEqual(self.extracted_paths(), ["a.js", "b.js"])
