"""The in-process tiers, the disk tier, filesystem checks and the lanes."""

import asyncio
import errno
import io
import logging
import os
import tempfile
import threading
import time
import zipfile
from unittest import mock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.symbolication import sources, store
from apps.symbolication.models import CacheKind


class FakeCache:
    def __init__(self, nbytes):
        self.nbytes = nbytes


MB = 1024 * 1024


@override_settings(GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB=1)
@mock.patch.object(store, "RAM_CACHE_IDLE_SECONDS", 600)
class MemoryTierTests(SimpleTestCase):
    def setUp(self):
        self.tier = store.LoadedCaches(
            capacity=lambda: settings.GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB * MB,
            idle=lambda: store.RAM_CACHE_IDLE_SECONDS,
            entry_limit=lambda capacity: capacity // 4,
        )
        self.addCleanup(self.tier.clear)

    def test_empty_until_used(self):
        self.assertEqual(len(self.tier), 0)
        self.assertEqual(self.tier.nbytes, 0)
        self.assertIsNone(self.tier._timer)
        self.assertIsNone(self.tier.get("missing"))

    def test_evicts_least_recently_used_by_bytes(self):
        for key in "abcd":
            self.tier.put(key, FakeCache(MB // 4), mapped=False)
        self.tier.get("a")  # a is now the most recently used
        self.tier.put("e", FakeCache(MB // 4), mapped=False)
        self.assertIsNone(self.tier.get("b"))
        self.assertIsNotNone(self.tier.get("a"))
        self.assertLessEqual(self.tier.nbytes, MB)

    def test_refuses_an_entry_over_a_quarter_of_capacity(self):
        self.assertFalse(self.tier.put("big", FakeCache(MB // 4 + 1), mapped=False))
        self.assertIsNone(self.tier.get("big"))
        self.assertEqual(self.tier.nbytes, 0)

    def test_mapped_caches_are_not_charged(self):
        """A memory-mapped cache's pages are the kernel's page cache: it
        costs the budget nothing, however big, but the entry count stays
        bounded."""
        self.assertTrue(self.tier.put("mapped", FakeCache(50 * MB), mapped=True))
        self.assertEqual(self.tier.nbytes, 0)
        with mock.patch.object(store, "MAX_LOADED_ENTRIES", 3):
            for index in range(5):
                self.tier.put(index, FakeCache(1), mapped=True)
        self.assertEqual(len(self.tier), 3)

    @override_settings(GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB=0)
    def test_zero_disables_it(self):
        self.assertFalse(self.tier.put("a", FakeCache(1), mapped=False))
        self.assertIsNone(self.tier.get("a"))

    def test_drains_to_empty_when_idle(self):
        self.tier.put("a", FakeCache(10), mapped=False)
        self.assertIsNotNone(self.tier._timer)
        with mock.patch.object(store, "RAM_CACHE_IDLE_SECONDS", 0):
            self.tier.sweep()
        self.assertEqual(len(self.tier), 0)
        self.assertEqual(self.tier.nbytes, 0)
        # With nothing left, no sweep is scheduled.
        self.assertIsNone(self.tier._timer)


@mock.patch.object(store, "OVERSIZED_MB", 64)
@mock.patch.object(store, "OVERSIZED_TTL_SECONDS", 60)
class OversizedTierTests(SimpleTestCase):
    def setUp(self):
        self.tier = store.LoadedCaches(
            capacity=lambda: store.OVERSIZED_MB * MB,
            idle=lambda: store.OVERSIZED_TTL_SECONDS,
            entry_limit=lambda capacity: capacity,
            admit_one_oversized=True,
        )
        self.addCleanup(self.tier.clear)

    def test_two_big_caches_fit_together(self):
        """Two large bundles of one event do not evict each other."""
        self.tier.put("a", FakeCache(30 * MB), mapped=False)
        self.tier.put("b", FakeCache(30 * MB), mapped=False)
        self.assertIsNotNone(self.tier.get("a"))
        self.assertIsNotNone(self.tier.get("b"))

    def test_always_admits_one_cache_however_big(self):
        self.tier.put("a", FakeCache(10 * MB), mapped=False)
        self.assertTrue(self.tier.put("huge", FakeCache(200 * MB), mapped=False))
        self.assertIsNotNone(self.tier.get("huge"))
        self.assertIsNone(self.tier.get("a"))
        self.assertEqual(len(self.tier), 1)

    def test_expires_after_its_ttl(self):
        self.tier.put("a", FakeCache(MB), mapped=False)
        with mock.patch.object(store, "OVERSIZED_TTL_SECONDS", 0.01):
            time.sleep(0.02)
            self.tier.sweep()
        self.assertEqual(len(self.tier), 0)

    def test_zero_ttl_disables_it(self):
        with mock.patch.object(store, "OVERSIZED_TTL_SECONDS", 0):
            self.assertFalse(self.tier.put("a", FakeCache(MB), mapped=False))


class NegativeCacheTests(SimpleTestCase):
    def test_remembers_then_forgets(self):
        cache = store.NegativeCache()
        with mock.patch.object(store, "UNAVAILABLE_SECONDS", 60):
            cache.put("k", "failed")
        self.assertEqual(cache.get("k"), "failed")
        with mock.patch.object(store, "UNAVAILABLE_SECONDS", 0.01):
            cache.put("k", "pending")
        time.sleep(0.02)
        self.assertIsNone(cache.get("k"))


class DiskTierTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        os.chmod(self.directory.name, 0o700)
        self.tier = store.DiskTier()
        # Small files and a small tier keep these tests fast.
        patch = mock.patch.object(store.DiskTier, "MIN_CAP_MB", 1)
        patch.start()
        self.addCleanup(patch.stop)
        override = override_settings(
            GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR=self.directory.name,
            GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB=1,
        )
        override.enable()
        self.addCleanup(override.disable)

    def files(self):
        return sorted(
            name for _, _, names in os.walk(self.directory.name) for name in names
        )

    def publish(self, key, size):
        return self.tier.publish(CacheKind.SYMCACHE, key, io.BytesIO(b"x" * size))

    @override_settings(GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR="")
    def test_off_by_default(self):
        self.assertIsNone(self.tier.directory())
        self.assertIsNone(self.publish("k", 1))
        self.assertIsNone(self.tier.load(CacheKind.SYMCACHE, "k", None))

    def test_publishes_whole_files_and_evicts_oldest_over_the_cap(self):
        first = self.publish("a", 500_000)
        os.utime(first, (1, 1))  # used long ago
        second = self.publish("b", 500_000)
        third = self.publish("c", 500_000)
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        self.assertTrue(os.path.exists(third))
        # No temporary files are left behind.
        self.assertEqual(len(self.files()), 2)

    def test_skips_a_cache_over_half_the_cap(self):
        """Keeping it would evict most of the tier on every publish."""
        self.assertIsNone(self.publish("big", MB // 2 + 1))
        self.assertEqual(self.files(), [])

    def test_never_evicts_the_file_it_just_wrote(self):
        """Even when it is the oldest-used (another process touched the
        rest since, or the clock moved), the fresh file stays."""
        future = time.time() + 3600
        others = [self.publish(key, 400_000) for key in ("a", "b")]
        for other in others:
            os.utime(other, (future, future))
        fresh = self.publish("fresh", 400_000)
        self.assertTrue(os.path.exists(fresh))
        self.assertEqual(sum(os.path.exists(other) for other in others), 1)

    def test_eviction_stays_in_its_own_subtree(self):
        """Files the tier did not write, and temporary files another process
        is still writing, are never evicted."""
        foreign = os.path.join(self.directory.name, "someone-elses-file")
        with open(foreign, "wb") as f:
            f.write(b"x" * 900_000)
        os.utime(foreign, (1, 1))
        in_progress = os.path.join(
            self.directory.name, store.cache_path(CacheKind.SYMCACHE, "t") + "x"
        )
        os.makedirs(os.path.dirname(in_progress), exist_ok=True)
        in_progress = os.path.join(os.path.dirname(in_progress), ".tmp-writing")
        with open(in_progress, "wb") as f:
            f.write(b"x" * 900_000)
        self.publish("a", 500_000)
        self.assertTrue(os.path.exists(foreign))
        self.assertTrue(os.path.exists(in_progress))

    def test_a_tier_too_small_is_off_and_said_once(self):
        # settings.py disables WARNING-and-below while testing; lift it.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        for cap_mb in (0, -5):
            with (
                self.subTest(cap_mb=cap_mb),
                override_settings(GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB=cap_mb),
                self.assertLogs("apps.symbolication.store", "WARNING") as logs,
            ):
                for _ in range(3):
                    self.assertIsNone(self.tier.directory())
                    self.assertIsNone(self.publish("a", 10))
                self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.files(), [])

    def test_refuses_a_directory_writable_by_others(self):
        os.chmod(self.directory.name, 0o777)
        self.assertIsNone(self.tier.directory())

    def test_refuses_a_directory_owned_by_another_user(self):
        with mock.patch.object(store.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(self.tier.directory())

    def test_a_failed_write_pauses_the_tier_then_it_recovers(self):
        with mock.patch.object(
            self.tier, "_write", side_effect=OSError(errno.EIO, "I/O error")
        ):
            self.assertIsNone(self.publish("a", 10))
        self.assertIsNone(self.tier.directory())  # paused
        self.tier._paused_until.clear()  # the pause ran out
        self.assertIsNotNone(self.publish("a", 10))

    def test_a_full_disk_is_evicted_and_retried(self):
        calls = []
        real_write = self.tier._write

        def write(path, source):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(path, source)

        with mock.patch.object(self.tier, "_write", side_effect=write):
            self.assertIsNotNone(self.publish("a", 10))
        self.assertEqual(len(calls), 2)

    def test_verifies_the_first_map_of_a_file_it_did_not_write(self):
        path = os.path.join(
            self.directory.name, store.cache_path(CacheKind.SYMCACHE, "k")
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x")
        loader = mock.MagicMock()
        with mock.patch.dict(store.LOADERS, {CacheKind.SYMCACHE: loader}):
            self.tier.load(CacheKind.SYMCACHE, "k", None)
            self.tier.load(CacheKind.SYMCACHE, "k", None)
        verified = [call.kwargs["verify"] for call in loader.load_file.call_args_list]
        self.assertEqual(verified, [True, False])

    def test_read_ahead_without_fadvise(self):
        path = os.path.join(self.directory.name, "f")
        with open(path, "wb") as f:
            f.write(b"x" * 200_000)
        with mock.patch.object(store, "os", mock.MagicMock(wraps=os)) as fake_os:
            del fake_os.posix_fadvise
            store._read_ahead(path)


class NodeLocalFilesystemTests(SimpleTestCase):
    def setUp(self):
        sources._filesystem_type.cache_clear()
        self.addCleanup(sources._filesystem_type.cache_clear)
        self.file = tempfile.NamedTemporaryFile()
        self.addCleanup(self.file.close)
        device = os.stat(self.file.name).st_dev
        self.device = f"{os.major(device)}:{os.minor(device)}"

    def with_mountinfo(self, fstype):
        mountinfo = tempfile.NamedTemporaryFile("w")
        self.addCleanup(mountinfo.close)
        mountinfo.write(
            f"36 35 {self.device} / /data rw,noatime shared:1 - {fstype} "
            "server:/export rw\n"
        )
        mountinfo.flush()
        sources._filesystem_type.cache_clear()
        return mock.patch.object(sources, "MOUNTINFO", mountinfo.name)

    def test_local_filesystems_are_node_local(self):
        for fstype in ("ext4", "xfs", "overlay", "tmpfs"):
            with self.subTest(fstype=fstype), self.with_mountinfo(fstype):
                self.assertTrue(sources.is_node_local(self.file.name))

    def test_network_filesystems_are_not(self):
        for fstype in ("nfs4", "cifs", "fuse.sshfs", "ceph", "glusterfs"):
            with self.subTest(fstype=fstype), self.with_mountinfo(fstype):
                self.assertFalse(sources.is_node_local(self.file.name))

    def test_unknown_or_other_platforms_are_not(self):
        with mock.patch.object(sources, "MOUNTINFO", "/nonexistent"):
            sources._filesystem_type.cache_clear()
            self.assertFalse(sources.is_node_local(self.file.name))
        with mock.patch.object(sources.sys, "platform", "darwin"):
            self.assertFalse(sources.is_node_local(self.file.name))


class LaneTests(SimpleTestCase):
    def test_work_runs_on_the_lanes_bounded_threads(self):
        async def names():
            return await asyncio.gather(
                *(
                    sources.load_lane.run(lambda: threading.current_thread().name)
                    for _ in range(10)
                )
            )

        thread_names = set(async_to_sync(names)())
        self.assertTrue(all(n.startswith("symbolication-read") for n in thread_names))
        self.assertLessEqual(len(thread_names), 2)

    def test_bundle_reads_have_their_own_lane(self):
        name = async_to_sync(sources.bundle_lane.run)(
            lambda: threading.current_thread().name
        )
        self.assertTrue(name.startswith("symbolication-bundle"))


class SourceBundleReadTests(SimpleTestCase):
    def bundle(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, text in members.items():
                zf.writestr(name, text)
        storage = mock.MagicMock()
        storage.open.side_effect = lambda *args: io.BytesIO(buffer.getvalue())
        return mock.MagicMock(id=1, blob=mock.MagicMock(storage=storage, name="b"))

    def test_skips_members_over_the_size_cap(self):
        blob = self.bundle({"files/small": "a\nb", "files/big": "x" * 100})
        with mock.patch.object(sources, "MAX_SOURCE_MEMBER_BYTES", 50):
            lines = sources.read_bundle_members(blob, ["files/small", "files/big"])
        self.assertEqual(lines, {"files/small": ["a", "b"]})

    def test_reads_at_most_the_path_cap(self):
        blob = self.bundle({f"files/{index}": "x" for index in range(10)})
        with mock.patch.object(sources, "MAX_SOURCE_PATHS", 3):
            lines = sources.read_bundle_members(
                blob, [f"files/{index}" for index in range(10)]
            )
        self.assertEqual(len(lines), 3)

    @override_settings(GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE=10)
    def test_refuses_an_oversized_archive_before_opening_it(self):
        blob = self.bundle({"files/a": "x"})
        with mock.patch.object(sources.zipfile, "ZipFile") as zip_file:
            self.assertEqual(sources.read_bundle_members(blob, ["files/a"]), {})
        zip_file.assert_not_called()
