import asyncio
import logging
import os
import tempfile
import threading
import uuid
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.event_ingest.process_event import process_issue_events
from apps.event_ingest.schema import ErrorIssueEventSchema, IssueTaskMessage
from apps.event_ingest.tests.symbolication import (
    ELF_DEBUG_ID,
    ELF_IMAGE_ADDR,
    IN_MEMORY_STORAGE,
    NATIVE_FIXTURES,
    crash_frame_offsets,
    measure_symbolication,
    store_blob,
    upload_native_dif,
)
from apps.event_ingest.tests.utils import EventIngestTestCase, run_async_closing
from apps.files.models import FileBlob
from apps.issue_events.models import IssueEvent

from . import store
from .maintenance import cleanup_symbolication_caches
from .models import CacheKind, CacheState, SymbolicationCache
from .sources import STORAGE_READ_THREADS, run_storage_read
from .tasks import build_cache


class FakeCache:
    def __init__(self, nbytes):
        self.nbytes = nbytes


@override_settings(
    GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB=1,
    GLITCHTIP_SYMBOLICATION_RAM_CACHE_IDLE_SECONDS=600,
)
class MemoryTierTests(SimpleTestCase):
    def setUp(self):
        self.tier = store.MemoryTier()
        self.addCleanup(self.tier.clear)

    def test_empty_until_used(self):
        self.assertEqual(len(self.tier), 0)
        self.assertEqual(self.tier.nbytes, 0)
        self.assertIsNone(self.tier._timer)
        self.assertIsNone(self.tier.get("missing"))

    def test_evicts_least_recently_used_by_bytes(self):
        quarter = 1024 * 1024 // 4
        for key in "abcd":
            self.tier.put(key, FakeCache(quarter))
        self.tier.get("a")  # a is now the most recently used
        self.tier.put("e", FakeCache(quarter))
        self.assertIsNone(self.tier.get("b"))
        self.assertIsNotNone(self.tier.get("a"))
        self.assertLessEqual(self.tier.nbytes, 1024 * 1024)

    def test_refuses_an_entry_over_a_quarter_of_capacity(self):
        self.tier.put("big", FakeCache(1024 * 1024 // 4 + 1))
        self.assertIsNone(self.tier.get("big"))
        self.assertEqual(self.tier.nbytes, 0)

    @override_settings(GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB=0)
    def test_zero_disables_it(self):
        self.tier.put("a", FakeCache(1))
        self.assertIsNone(self.tier.get("a"))

    def test_drains_to_empty_when_idle(self):
        self.tier.put("a", FakeCache(10))
        self.assertIsNotNone(self.tier._timer)
        with override_settings(GLITCHTIP_SYMBOLICATION_RAM_CACHE_IDLE_SECONDS=0):
            self.tier.sweep()
        self.assertEqual(len(self.tier), 0)
        self.assertEqual(self.tier.nbytes, 0)
        # With nothing left, no sweep is scheduled.
        self.assertIsNone(self.tier._timer)


class DiskTierTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tier = store.DiskTier()

    def files(self):
        return sorted(
            name for _, _, names in os.walk(self.directory.name) for name in names
        )

    def test_off_by_default(self):
        self.assertIsNone(self.tier.directory())
        self.assertIsNone(self.tier.publish(CacheKind.SYMCACHE, "k", b"data"))
        self.assertIsNone(self.tier.load(CacheKind.SYMCACHE, "k"))

    def test_publishes_whole_files_and_evicts_oldest_over_the_cap(self):
        with override_settings(
            GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR=self.directory.name,
            GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB=1,
        ):
            first = self.tier.publish(CacheKind.SYMCACHE, "a", b"a" * 600_000)
            os.utime(first, (1, 1))  # used long ago
            second = self.tier.publish(CacheKind.SYMCACHE, "b", b"b" * 600_000)
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        # No temporary files are left behind.
        self.assertEqual(len(self.files()), 1)


class StorageReadPoolTests(SimpleTestCase):
    def test_reads_run_on_the_bounded_pool(self):
        async def names():
            return await asyncio.gather(
                *(
                    run_storage_read(lambda: threading.current_thread().name)
                    for _ in range(10)
                )
            )

        thread_names = set(async_to_sync(names)())
        self.assertTrue(all(n.startswith("symbolication-read") for n in thread_names))
        self.assertLessEqual(len(thread_names), STORAGE_READ_THREADS)


def native_crash() -> dict:
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": timezone.now().isoformat(),
        "platform": "native",
        "level": "fatal",
        "contexts": {"os": {"name": "Linux"}, "device": {"arch": "x86_64"}},
        "debug_meta": {
            "images": [
                {
                    "type": "elf",
                    "debug_id": ELF_DEBUG_ID,
                    "image_addr": hex(ELF_IMAGE_ADDR),
                }
            ]
        },
        "exception": {
            "values": [
                {
                    "type": "SIGSEGV",
                    "value": "Segfault",
                    "stacktrace": {
                        "frames": [
                            {"instruction_addr": hex(ELF_IMAGE_ADDR + offset)}
                            for offset in crash_frame_offsets("elf")
                        ]
                    },
                }
            ]
        },
    }


@IN_MEMORY_STORAGE
class CacheLifecycleTests(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(store.disk_tier.reset)
        store.disk_tier.reset()

    def upload_elf(self, content=None):
        return upload_native_dif(
            self.project,
            "crash.elf",
            content or (NATIVE_FIXTURES / "crash.elf").read_bytes(),
            "lib",
            ELF_DEBUG_ID,
        )

    def process(self, payload) -> IssueEvent:
        run_async_closing(
            process_issue_events,
            [
                IssueTaskMessage(
                    project_id=self.project.id,
                    organization_id=self.organization.id,
                    received=timezone.now(),
                    payload=ErrorIssueEventSchema(**payload),
                )
            ],
        )
        return IssueEvent.objects.get_event(payload["event_id"])

    def resolved(self, event: IssueEvent) -> bool:
        frames = event.data["exception"]["values"][0]["stacktrace"]["frames"]
        return any(frame.get("filename") for frame in frames)

    def test_build_is_stored_and_single_flight(self):
        self.upload_elf()
        with measure_symbolication() as cost:
            event = self.process(native_crash())
        self.assertTrue(self.resolved(event))
        row = SymbolicationCache.objects.get()
        self.assertEqual(row.state, CacheState.READY)
        self.assertEqual(row.kind, CacheKind.SYMCACHE)
        self.assertTrue(row.storage_name.startswith("uploads/symbolcache/symcache/"))
        self.assertTrue(default_storage.exists(row.storage_name))
        self.assertEqual(row.size, default_storage.size(row.storage_name))
        self.assertEqual(cost.cache_builds, 1)

        # Queued again (a duplicate task, another pod): the claim fails, so
        # nothing is rebuilt.
        with measure_symbolication() as cost:
            async_to_sync(build_cache.func)(row.id)
        self.assertEqual(cost.cache_builds, 0)

    def test_concurrent_builds_of_one_row_run_once(self):
        dif = self.upload_elf()
        row = SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE,
            cache_key=f"{dif.file.blob.checksum}:{ELF_DEBUG_ID}:x86_64",
            format_tag=store.format_tag(CacheKind.SYMCACHE),
            source_blob=dif.file.blob,
        )

        async def race():
            await asyncio.gather(*(build_cache.func(row.id) for _ in range(4)))

        with measure_symbolication() as cost:
            run_async_closing(race)
        self.assertEqual(cost.cache_builds, 1)

    def test_unbuildable_source_is_not_retried(self):
        self.upload_elf()
        blob = FileBlob.objects.get()
        # Replace the stored bytes with something that is not a debug file.
        default_storage.delete(blob.blob.name)
        default_storage.save(blob.blob.name, ContentFile(b"not a debug file"))
        event = self.process(native_crash())
        self.assertFalse(self.resolved(event))
        row = SymbolicationCache.objects.get()
        self.assertEqual(row.state, CacheState.FAILED)
        self.assertEqual(row.attempts, store.MAX_ATTEMPTS)
        self.assertTrue(row.error)

        with measure_symbolication() as cost:
            self.process(native_crash())
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(cost.files_opened, 0)
        self.assertEqual(cost.cache_unavailable, 1)

    @override_settings(GLITCHTIP_SYMBOLICATION_BUILD_WAIT_SECONDS=0.05)
    def test_event_goes_on_unsymbolicated_when_the_build_is_slow(self):
        self.upload_elf()
        # The build task is queued but no worker runs it in time.
        with (
            mock.patch.object(store, "_enqueue", mock.AsyncMock()) as enqueue,
            measure_symbolication() as cost,
        ):
            event = self.process(native_crash())
        enqueue.assert_awaited_once()
        self.assertFalse(self.resolved(event))
        self.assertEqual(cost.cache_unavailable, 1)
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.PENDING)

        # When the build lands, later events use it.
        async_to_sync(build_cache.func)(SymbolicationCache.objects.get().id)
        self.assertTrue(self.resolved(self.process(native_crash())))

    def test_stale_building_claim_is_taken_over(self):
        dif = self.upload_elf()
        row = SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE,
            cache_key=f"{dif.file.blob.checksum}:{ELF_DEBUG_ID}:x86_64",
            format_tag=store.format_tag(CacheKind.SYMCACHE),
            source_blob=dif.file.blob,
            state=CacheState.BUILDING,
        )
        SymbolicationCache.objects.filter(id=row.id).update(
            updated=timezone.now() - store.STALE_BUILD * 2
        )
        async_to_sync(build_cache.func)(row.id)
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.READY)

    def test_cleanup_removes_caches_of_deleted_sources(self):
        self.upload_elf()
        self.process(native_crash())
        row = SymbolicationCache.objects.get()
        self.assertTrue(default_storage.exists(row.storage_name))

        # Deleting the source's blob (retention, project deletion, orphan
        # cleanup) unlinks the cache, and maintenance deletes it.
        FileBlob.objects.filter(id=row.source_blob_id).delete()
        async_to_sync(cleanup_symbolication_caches)()
        self.assertFalse(SymbolicationCache.objects.exists())
        self.assertFalse(default_storage.exists(row.storage_name))

    def test_cleanup_removes_caches_in_an_old_format(self):
        blob = store_blob("x", b"x")
        name = "uploads/symbolcache/symcache/0.0/abc"
        default_storage.save(name, ContentFile(b"old cache"))
        SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE,
            cache_key="k",
            format_tag="0.0",
            source_blob=blob,
            state=CacheState.READY,
            storage_name=name,
        )
        current = SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE,
            cache_key="k",
            format_tag=store.format_tag(CacheKind.SYMCACHE),
            source_blob=blob,
        )
        async_to_sync(cleanup_symbolication_caches)()
        self.assertEqual(list(SymbolicationCache.objects.all()), [current])
        self.assertFalse(default_storage.exists(name))

    def test_disk_tier_off_on_and_unwritable(self):
        self.upload_elf()
        self.process(native_crash())  # builds and stores the cache

        # Off (the default): a cold process loads from file storage.
        with measure_symbolication() as cost:
            self.assertTrue(self.resolved(self.process(native_crash())))
        self.assertEqual(cost.cache_storage_loads, 1)
        self.assertEqual(cost.cache_disk_hits, 0)

        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR=directory),
        ):
            # On: the first cold load copies the cache to local disk...
            with measure_symbolication() as cost:
                self.process(native_crash())
            self.assertEqual(cost.cache_storage_loads, 1)
            local = [
                os.path.join(root, name)
                for root, _, names in os.walk(directory)
                for name in names
            ]
            self.assertEqual(len(local), 1)
            # ...and the next cold process maps it, without storage.
            with measure_symbolication() as cost:
                self.assertTrue(self.resolved(self.process(native_crash())))
            self.assertEqual(cost.cache_disk_hits, 1)
            self.assertEqual(cost.cache_storage_loads, 0)
            self.assertEqual(cost.files_opened, 0)

        # Unwritable: logged once, and symbolication carries on from storage.
        # settings.py disables WARNING-and-below while testing; lift it.
        previous_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous_disable)
        with tempfile.NamedTemporaryFile() as not_a_directory:
            unusable = os.path.join(not_a_directory.name, "cache")
            with (
                override_settings(GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR=unusable),
                self.assertLogs("apps.symbolication.store", "WARNING") as logs,
            ):
                for _ in range(2):
                    with measure_symbolication() as cost:
                        self.assertTrue(self.resolved(self.process(native_crash())))
                    self.assertEqual(cost.cache_storage_loads, 1)
            self.assertEqual(len(logs.records), 1)
