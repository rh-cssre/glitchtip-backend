"""Building, loading and deleting caches, through the database and storage."""

import asyncio
import contextlib
import logging
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings
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
from apps.symbolication import maintenance, sources, store, tasks
from apps.symbolication.maintenance import cleanup_symbolication_caches
from apps.symbolication.models import CacheKind, CacheState, SymbolicationCache
from apps.symbolication.tasks import build_cache


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


@contextlib.contextmanager
def counted_waits():
    """The delays the store sleeps for while polling a build, in order;
    the sleeps still happen. Counting them keeps budget assertions off the
    wall clock, which also counts database round trips."""
    waits = []

    class CountingAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay, *args, **kwargs):
            waits.append(delay)
            return await asyncio.sleep(delay, *args, **kwargs)

    with mock.patch.object(store, "asyncio", CountingAsyncio()):
        yield waits


def lift_log_disable(test):
    """settings.py disables WARNING-and-below while testing; lift it."""
    previous = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    test.addCleanup(logging.disable, previous)


@IN_MEMORY_STORAGE
class CacheTestCase(EventIngestTestCase):
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

    def source_for(self, dif) -> store.CacheSource:
        return store.CacheSource(
            kind=CacheKind.SYMCACHE,
            cache_key=f"{dif.file.blob.checksum}:{ELF_DEBUG_ID}:x86_64",
            source_blob_id=dif.file.blob.id,
        )

    def row_for(self, dif, **fields) -> SymbolicationCache:
        source = self.source_for(dif)
        return SymbolicationCache.objects.create(
            kind=source.kind,
            cache_key=source.cache_key,
            format_tag=store.format_tag(source.kind),
            source_blob_id=source.source_blob_id,
            **fields,
        )

    def get(self, source, context=None):
        return async_to_sync(store.get_cache)(source, context)

    def build(self, row_id):
        async_to_sync(build_cache.func)(row_id)

    def process(self, *payloads) -> list[IssueEvent]:
        run_async_closing(
            process_issue_events,
            [
                IssueTaskMessage(
                    project_id=self.project.id,
                    organization_id=self.organization.id,
                    received=timezone.now(),
                    payload=ErrorIssueEventSchema(**payload),
                )
                for payload in payloads
            ],
        )
        return [IssueEvent.objects.get_event(p["event_id"]) for p in payloads]

    def resolved(self, event: IssueEvent) -> bool:
        frames = event.data["exception"]["values"][0]["stacktrace"]["frames"]
        return any(frame.get("filename") for frame in frames)


class BuildTests(CacheTestCase):
    def test_build_is_stored_under_a_fresh_name_and_is_single_flight(self):
        self.upload_elf()
        with measure_symbolication() as cost:
            [event] = self.process(native_crash())
        self.assertTrue(self.resolved(event))
        row = SymbolicationCache.objects.get()
        self.assertEqual(row.state, CacheState.READY)
        self.assertEqual(row.requested_by_organization_id, self.organization.id)
        self.assertTrue(
            row.storage_name.startswith(
                store.cache_path(CacheKind.SYMCACHE, row.cache_key) + "-"
            )
        )
        self.assertTrue(default_storage.exists(row.storage_name))
        self.assertEqual(row.size, default_storage.size(row.storage_name))
        self.assertEqual(cost.cache_builds, 1)

        # Queued again (a duplicate task, another pod): the claim fails.
        with measure_symbolication() as cost:
            self.build(row.id)
        self.assertEqual(cost.cache_builds, 0)

    def test_concurrent_builds_of_one_row_run_once(self):
        row = self.row_for(self.upload_elf())

        async def race():
            await asyncio.gather(*(build_cache.func(row.id) for _ in range(4)))

        with measure_symbolication() as cost:
            run_async_closing(race)
        self.assertEqual(cost.cache_builds, 1)

    def test_a_rebuild_replaces_the_old_bytes(self):
        row = self.row_for(self.upload_elf())
        self.build(row.id)
        first = SymbolicationCache.objects.get().storage_name
        SymbolicationCache.objects.filter(id=row.id).update(state=CacheState.PENDING)
        self.build(row.id)
        second = SymbolicationCache.objects.get().storage_name
        self.assertNotEqual(first, second)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(second))

    def test_a_build_whose_row_moved_on_deletes_its_bytes(self):
        """If the row stops being BUILDING while the build runs (maintenance
        deleted it, or it was taken over), the build must not overwrite it
        and must not leave its bytes behind."""
        row = self.row_for(self.upload_elf())
        real_build = tasks._build
        stored = []

        def build_then_lose_the_row(row_arg, nonce):
            SymbolicationCache.objects.filter(id=row.id).update(
                state=CacheState.FAILED, error="taken over"
            )
            name, size = real_build(row_arg, nonce)
            stored.append(name)
            return name, size

        with mock.patch.object(tasks, "_build", build_then_lose_the_row):
            self.build(row.id)
        row.refresh_from_db()
        self.assertEqual(row.state, CacheState.FAILED)
        self.assertEqual(row.error, "taken over")
        self.assertFalse(default_storage.exists(stored[0]))

    def test_unbuildable_input_is_not_retried(self):
        self.upload_elf()
        blob = FileBlob.objects.get()
        default_storage.delete(blob.blob.name)
        default_storage.save(blob.blob.name, ContentFile(b"not a debug file"))
        [event] = self.process(native_crash())
        self.assertFalse(self.resolved(event))
        row = SymbolicationCache.objects.get()
        self.assertEqual(row.state, CacheState.FAILED)
        self.assertEqual(row.attempts, store.MAX_ATTEMPTS)

        store.clear_process_caches()
        SymbolicationCache.objects.update(updated=timezone.now() - timedelta(days=1))
        with measure_symbolication() as cost:
            self.process(native_crash())
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(cost.files_opened, 0)

    def test_transient_errors_are_retried(self):
        row = self.row_for(self.upload_elf())
        with mock.patch.object(tasks, "_build", side_effect=OSError("disk gone")):
            self.build(row.id)
        row.refresh_from_db()
        self.assertEqual(row.state, CacheState.FAILED)
        self.assertLess(row.attempts, store.MAX_ATTEMPTS)
        # After the retry delay, the next event requeues it.
        SymbolicationCache.objects.filter(id=row.id).update(
            updated=timezone.now() - store.RETRY_FAILED_AFTER * 2
        )
        self.assertIsNotNone(self.get(self.source_for(self.upload_elf())))
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.READY)

    @override_settings(GLITCHTIP_SYMBOLICATION_BUILD_MEMORY_MB=0)
    def test_inputs_over_the_size_cap_are_refused_for_good(self):
        row = self.row_for(self.upload_elf())
        with mock.patch.object(tasks, "_build") as build:
            self.build(row.id)
        build.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.state, CacheState.FAILED)
        self.assertEqual(row.attempts, store.MAX_ATTEMPTS)

    def test_a_cancelled_build_goes_back_to_pending(self):
        row = self.row_for(self.upload_elf())
        with (
            mock.patch.object(tasks, "_run_build", side_effect=asyncio.CancelledError),
            self.assertRaises(asyncio.CancelledError),
        ):
            self.build(row.id)
        row.refresh_from_db()
        self.assertEqual(row.state, CacheState.PENDING)

    def test_a_long_build_keeps_its_claim_fresh(self):
        row = self.row_for(self.upload_elf())
        real_build = tasks._build
        seen = []

        def slow_build(row_arg, nonce):
            before = SymbolicationCache.objects.get(id=row.id).updated
            time.sleep(0.3)
            seen.append((before, SymbolicationCache.objects.get(id=row.id).updated))
            return real_build(row_arg, nonce)

        with (
            mock.patch.object(tasks, "HEARTBEAT_SECONDS", 0.05),
            mock.patch.object(tasks, "_build", slow_build),
        ):
            self.build(row.id)
        before, during = seen[0]
        self.assertGreater(during, before)

    def test_a_dead_claim_is_taken_over_until_attempts_run_out(self):
        dif = self.upload_elf()
        row = self.row_for(dif, state=CacheState.BUILDING, attempts=1)
        stale = timezone.now() - store.STALE_BUILD * 2
        SymbolicationCache.objects.filter(id=row.id).update(updated=stale)
        self.build(row.id)
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.READY)

        SymbolicationCache.objects.filter(id=row.id).update(
            state=CacheState.BUILDING, attempts=store.MAX_ATTEMPTS, updated=stale
        )
        lift_log_disable(self)
        with self.assertLogs("apps.symbolication.tasks", "WARNING"):
            self.build(row.id)
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.FAILED)

    def test_a_killed_build_is_taken_over_within_minutes(self):
        """A pod killed mid-build leaves its row BUILDING with no heartbeat;
        three missed heartbeats later the next event takes it over."""
        dif = self.upload_elf()
        row = self.row_for(dif, state=CacheState.BUILDING, attempts=1)
        self.assertLessEqual(store.STALE_BUILD, store.BUILD_HEARTBEAT * 3)
        SymbolicationCache.objects.filter(id=row.id).update(
            updated=timezone.now() - store.BUILD_HEARTBEAT * 3 - timedelta(seconds=5)
        )
        self.assertIsNotNone(self.get(self.source_for(dif)))
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.READY)

    def test_a_build_still_heartbeating_is_not_taken_over(self):
        dif = self.upload_elf()
        row = self.row_for(dif, state=CacheState.BUILDING, attempts=1)
        SymbolicationCache.objects.filter(id=row.id).update(
            updated=timezone.now() - store.BUILD_HEARTBEAT
        )
        with measure_symbolication() as cost:
            self.build(row.id)
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.BUILDING)

    def test_memory_is_returned_after_each_build(self):
        row = self.row_for(self.upload_elf())
        with mock.patch.object(tasks, "malloc_trim") as trim:
            self.build(row.id)
        trim.assert_called_once()

    def test_builds_fall_back_to_memory_without_a_temp_dir(self):
        """A read-only root with no writable /tmp: builds read their inputs
        into memory, and say so once."""
        lift_log_disable(self)
        read_only = OSError(30, "Read-only file system")
        with (
            mock.patch.object(sources, "_warned_no_temp_dir", False),
            mock.patch.object(
                sources.tempfile, "NamedTemporaryFile", side_effect=read_only
            ),
            mock.patch.object(
                tasks.tempfile, "TemporaryDirectory", side_effect=read_only
            ),
            self.assertLogs("apps.symbolication.sources", "WARNING") as logs,
        ):
            for name in ("crash.elf", "crash.macho.dwarf"):
                blob = store_blob(name, (NATIVE_FIXTURES / name).read_bytes())
                row = SymbolicationCache.objects.create(
                    kind=CacheKind.SYMCACHE,
                    cache_key=f"{blob.checksum}::",
                    format_tag=store.format_tag(CacheKind.SYMCACHE),
                    source_blob=blob,
                )
                self.build(row.id)
                row.refresh_from_db()
                self.assertEqual(row.state, CacheState.READY, row.error)
        self.assertEqual(len(logs.records), 1)


class LookupTests(CacheTestCase):
    def test_a_cache_built_from_other_bytes_is_rejected(self):
        """The stored header records its source's digest: a cache that does
        not match its row's source is not used, but rebuilt."""
        dif = self.upload_elf()
        other = store_blob(
            "other.elf", (NATIVE_FIXTURES / "crash.elf").read_bytes() + b"\0"
        )
        row = self.row_for(dif)
        # Build from the other blob, then point the row back at its source.
        SymbolicationCache.objects.filter(id=row.id).update(source_blob=other)
        with mock.patch.object(
            tasks, "source_digest", return_value=bytes.fromhex(other.checksum)
        ):
            self.build(row.id)
        SymbolicationCache.objects.filter(id=row.id).update(source_blob=dif.file.blob)
        store.clear_process_caches()
        self.assertIsNone(self.get(self.source_for(dif)))
        # Sent back to be rebuilt from the right source (here at once).
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.READY)
        store.clear_process_caches()
        self.assertIsNotNone(self.get(self.source_for(dif)))

    def test_a_corrupt_cache_is_deleted_and_rebuilt(self):
        dif = self.upload_elf()
        self.assertIsNotNone(self.get(self.source_for(dif)))
        row = SymbolicationCache.objects.get()
        with default_storage.open(row.storage_name, "rb") as stored:
            data = bytearray(stored.read())
        data[-1] ^= 0xFF  # damage the payload, so verification fails
        default_storage.delete(row.storage_name)
        default_storage.save(row.storage_name, ContentFile(bytes(data)))
        store.clear_process_caches()

        with measure_symbolication() as cost:
            self.assertIsNone(self.get(self.source_for(dif)))
        self.assertFalse(default_storage.exists(row.storage_name))
        self.assertEqual(cost.cache_builds, 1)
        store.clear_process_caches()
        self.assertIsNotNone(self.get(self.source_for(dif)))

    def test_a_corrupt_cache_rebuilt_meanwhile_is_left_alone(self):
        """Another event already rebuilt it: the stale view must not delete
        the new build's bytes."""
        dif = self.upload_elf()
        self.assertIsNotNone(self.get(self.source_for(dif)))
        stale_view = SymbolicationCache.objects.get()
        SymbolicationCache.objects.filter(id=stale_view.id).update(
            storage_name="uploads/symbolcache/newer"
        )
        default_storage.save("uploads/symbolcache/newer", ContentFile(b"x"))
        async_to_sync(store._rebuild)(stale_view, None, True)
        self.assertTrue(default_storage.exists(stale_view.storage_name))
        self.assertTrue(default_storage.exists("uploads/symbolcache/newer"))
        self.assertEqual(SymbolicationCache.objects.get().state, CacheState.READY)

    def test_a_row_whose_source_was_deleted_is_repointed(self):
        """The source was deleted (the FK went NULL) and uploaded again."""
        dif = self.upload_elf()
        row = self.row_for(dif, state=CacheState.FAILED, attempts=store.MAX_ATTEMPTS)
        SymbolicationCache.objects.filter(id=row.id).update(source_blob=None)
        self.assertIsNotNone(self.get(self.source_for(dif)))
        row.refresh_from_db()
        self.assertEqual(row.source_blob_id, dif.file.blob.id)
        self.assertEqual(row.state, CacheState.READY)

    def test_a_lost_build_is_requeued_once_however_many_events_see_it(self):
        dif = self.upload_elf()
        row = self.row_for(dif)
        SymbolicationCache.objects.filter(id=row.id).update(
            updated=timezone.now() - store.REQUEUE_AFTER * 2
        )

        async def many():
            await asyncio.gather(
                *(
                    store.get_cache(
                        self.source_for(dif), store.LookupContext.for_event()
                    )
                    for _ in range(5)
                )
            )

        with (
            mock.patch.object(store, "BUILD_WAIT_SECONDS", 0.05),
            mock.patch.object(store, "_enqueue", mock.AsyncMock()) as enqueue,
        ):
            run_async_closing(many)
        self.assertEqual(enqueue.await_count, 1)

    @mock.patch.object(store, "BUILD_WAIT_SECONDS", 0.2)
    def test_an_event_shares_one_wait_budget_over_its_caches(self):
        sources_ = []
        for index in range(3):
            blob = store_blob(f"f{index}", f"content {index}".encode())
            sources_.append(
                store.CacheSource(CacheKind.SYMCACHE, f"{blob.checksum}::", blob.id)
            )
        context = store.LookupContext.for_event()

        async def lookups():
            for source in sources_:
                await store.get_cache(source, context)

        with (
            mock.patch.object(store, "_enqueue", mock.AsyncMock()),
            counted_waits() as waits,
        ):
            run_async_closing(lookups)
        # All three waited, within one budget between them; a budget per
        # cache would sleep up to three times as long.
        self.assertGreater(len(waits), 0)
        self.assertLessEqual(sum(waits), 0.2 + 1e-6)

    @mock.patch.object(store, "BUILD_WAIT_SECONDS", 0.2)
    def test_a_key_given_up_on_is_not_waited_for_again(self):
        dif = self.upload_elf()
        with mock.patch.object(store, "_enqueue", mock.AsyncMock()):
            with counted_waits() as waits:
                self.assertIsNone(self.get(self.source_for(dif)))
            self.assertGreater(len(waits), 0)
            # The rest of the process: a quick look, no wait.
            with counted_waits() as waits:
                self.assertIsNone(self.get(self.source_for(dif)))
            self.assertEqual(waits, [])
        # Once built, that quick look finds it.
        self.build(SymbolicationCache.objects.get().id)
        self.assertIsNotNone(self.get(self.source_for(dif)))

    def test_the_batch_does_not_ask_again_for_a_key_it_gave_up_on(self):
        dif = self.upload_elf()
        unavailable = {
            (
                CacheKind.SYMCACHE,
                store.format_tag(CacheKind.SYMCACHE),
                self.source_for(dif).cache_key,
            )
        }
        context = store.LookupContext.for_event(batch_unavailable=unavailable)
        with measure_symbolication() as cost:
            self.assertIsNone(self.get(self.source_for(dif), context))
        self.assertFalse(SymbolicationCache.objects.exists())
        self.assertEqual(cost.cache_misses, 0)

    @mock.patch.object(store, "MAX_PENDING_PER_ORG", 1)
    @mock.patch.object(store, "BUILD_WAIT_SECONDS", 0)
    def test_one_organization_cannot_fill_the_build_queue(self):
        blobs = [store_blob(f"f{index}", f"x{index}".encode()) for index in range(2)]
        context = store.LookupContext.for_event(organization_id=self.organization.id)
        with mock.patch.object(store, "_enqueue", mock.AsyncMock()) as enqueue:
            for blob in blobs:
                self.get(
                    store.CacheSource(
                        CacheKind.SYMCACHE, f"{blob.checksum}::", blob.id
                    ),
                    context,
                )
        self.assertEqual(SymbolicationCache.objects.count(), 1)
        self.assertEqual(enqueue.await_count, 1)

    def test_lookup_errors_never_fail_ingest(self):
        self.upload_elf()
        lift_log_disable(self)
        with (
            mock.patch.object(
                store, "_request_build", side_effect=RuntimeError("storage down")
            ),
            self.assertLogs("apps.symbolication.store", "WARNING"),
        ):
            [event] = self.process(native_crash())
        self.assertFalse(self.resolved(event))

    def test_symbolication_errors_never_fail_ingest(self):
        self.upload_elf()
        lift_log_disable(self)
        with (
            mock.patch(
                "apps.event_ingest.process_event.resolve_event_stacktraces",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs("apps.event_ingest.process_event", "WARNING"),
        ):
            [event] = self.process(native_crash())
        self.assertIsNotNone(event)

    def test_cancelling_one_waiter_leaves_the_shared_load_running(self):
        """Two events wait on one load still queued on the load lane, and
        one is cancelled. Unshielded, that would cancel the queued load
        itself, and the other event with it."""
        dif = self.upload_elf()
        self.assertIsNotNone(self.get(self.source_for(dif)))
        store.clear_process_caches()
        # Occupy every thread of the lane, so the load stays queued.
        release = threading.Event()
        self.addCleanup(release.set)
        for _ in range(sources.load_lane._threads):
            sources.load_lane.submit(release.wait, 10)
        key = (
            CacheKind.SYMCACHE,
            store.format_tag(CacheKind.SYMCACHE),
            self.source_for(dif).cache_key,
        )

        async def scenario():
            first = asyncio.create_task(store.get_cache(self.source_for(dif)))
            second = asyncio.create_task(store.get_cache(self.source_for(dif)))
            # Both are waiting once the queued load has both their callbacks
            # (the leader also adds one to forget the load when it is done).
            while (
                key not in store._loads_in_flight
                or len(store._loads_in_flight[key]._done_callbacks) < 3
            ):
                await asyncio.sleep(0.01)
            self.assertFalse(store._loads_in_flight[key].running())
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
            release.set()
            return await second

        self.assertIsNotNone(run_async_closing(scenario))

    def test_a_load_that_finishes_at_once_does_not_deadlock(self):
        """The load can be done before its completion callback is added;
        the callback then runs at once, in the caller's thread."""
        dif = self.upload_elf()
        self.assertIsNotNone(self.get(self.source_for(dif)))
        store.clear_process_caches()
        real_submit = sources.load_lane.submit

        def submit_and_finish(func, *args):
            future = real_submit(func, *args)
            future.result()
            return future

        with mock.patch.object(store.load_lane, "submit", submit_and_finish):
            result = []
            worker = threading.Thread(
                target=lambda: result.append(self.get(self.source_for(dif)))
            )
            worker.start()
            worker.join(10)
        self.assertFalse(worker.is_alive(), "deadlocked")
        self.assertIsNotNone(result[0])
        self.assertEqual(store._loads_in_flight, {})

    def test_concurrent_events_share_one_storage_load(self):
        dif = self.upload_elf()
        self.assertIsNotNone(self.get(self.source_for(dif)))
        store.clear_process_caches()
        release = threading.Event()
        real = store._load_from_tiers

        def gated(*args):
            release.wait(5)
            return real(*args)

        async def many():
            waiting = [
                asyncio.create_task(store.get_cache(self.source_for(dif)))
                for _ in range(5)
            ]
            while len(store._loads_in_flight) < 1:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # let the rest join the load in flight
            release.set()
            return await asyncio.gather(*waiting)

        with (
            mock.patch.object(store, "_load_from_tiers", gated),
            measure_symbolication(cold=False) as cost,
        ):
            caches = run_async_closing(many)
        self.assertTrue(all(caches))
        self.assertEqual(cost.cache_storage_loads, 1)
        self.assertEqual(cost.cache_shared_loads, 4)

    @override_settings(GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB=0)
    @mock.patch.object(store, "OVERSIZED_TTL_SECONDS", 60)
    def test_a_cache_too_big_for_memory_is_kept_briefly_for_reuse(self):
        dif = self.upload_elf()
        self.get(self.source_for(dif))
        store.clear_process_caches()
        # Downloaded from (in-memory) storage into bytes: not a mapped cache.
        with mock.patch.object(store, "_load_via_temp_file", side_effect=_bytes_load):
            with measure_symbolication() as cost:
                self.get(self.source_for(dif))
            self.assertEqual(cost.cache_storage_loads, 1)
            self.assertEqual(len(store.oversized_tier), 1)
            # A repeat within the TTL reuses it: no storage read.
            with measure_symbolication(cold=False) as cost:
                self.assertIsNotNone(self.get(self.source_for(dif)))
            self.assertEqual(cost.files_opened, 0)
            self.assertEqual(cost.cache_hits, 1)


def _bytes_load(loader, stored, digest):
    return (
        loader.load(stored.read(), verify=True, expected_source_digest=digest),
        "storage_load",
        False,
    )


class StoredCacheLoadTests(CacheTestCase):
    """How a READY cache is loaded when it is not in memory."""

    def test_remote_storage_is_downloaded_and_mapped(self):
        dif = self.upload_elf()
        self.get(self.source_for(dif))
        store.clear_process_caches()
        with measure_symbolication() as cost:
            self.assertIsNotNone(self.get(self.source_for(dif)))
        self.assertEqual(cost.cache_storage_loads, 1)
        self.assertEqual(cost.cache_storage_maps, 0)
        self.assertEqual(cost.rust_calls["SymCache.load"], 0)
        self.assertEqual(cost.rust_calls["SymCache.load_file"], 1)


class LocalStorageLoadTests(EventIngestTestCase):
    """With local file storage the stored cache is memory-mapped in place."""

    def setUp(self):
        super().setUp()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(
            MEDIA_ROOT=media.name,
            STORAGES={
                **django_settings.STORAGES,
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            },
        )
        override.enable()
        self.addCleanup(override.disable)
        self.dif = upload_native_dif(
            self.project,
            "crash.elf",
            (NATIVE_FIXTURES / "crash.elf").read_bytes(),
            "lib",
            ELF_DEBUG_ID,
        )
        self.source = store.CacheSource(
            kind=CacheKind.SYMCACHE,
            cache_key=f"{self.dif.file.blob.checksum}:{ELF_DEBUG_ID}:x86_64",
            source_blob_id=self.dif.file.blob.id,
        )

    def test_stored_cache_is_mapped_not_read(self):
        with mock.patch.object(store, "is_node_local", return_value=True):
            with measure_symbolication() as cost:
                self.assertIsNotNone(async_to_sync(store.get_cache)(self.source))
            self.assertEqual(cost.cache_builds, 1)
            # The build mapped its local source instead of opening it, and
            # the cache was mapped where it was stored.
            self.assertEqual(cost.cache_storage_maps, 1)
            self.assertEqual(cost.files_opened, 0)

            with measure_symbolication() as cost:
                self.assertIsNotNone(async_to_sync(store.get_cache)(self.source))
            self.assertEqual(cost.cache_storage_maps, 1)
            self.assertEqual(cost.bytes_read, 0)

    def test_a_network_filesystem_is_not_mapped_in_place(self):
        with mock.patch.object(store, "is_node_local", return_value=False):
            async_to_sync(store.get_cache)(self.source)
            store.clear_process_caches()
            with measure_symbolication() as cost:
                self.assertIsNotNone(async_to_sync(store.get_cache)(self.source))
        self.assertEqual(cost.cache_storage_maps, 0)
        self.assertEqual(cost.cache_storage_loads, 1)


class DiskTierIntegrationTests(CacheTestCase):
    def test_off_on_and_unwritable(self):
        dif = self.upload_elf()
        self.get(self.source_for(dif))  # builds and stores the cache

        # Off (the default): a cold process downloads from file storage.
        store.clear_process_caches()
        with measure_symbolication() as cost:
            self.assertIsNotNone(self.get(self.source_for(dif)))
        self.assertEqual(cost.cache_storage_loads, 1)
        self.assertEqual(cost.cache_disk_hits, 0)

        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR=directory),
        ):
            # On: the first cold load copies the cache to local disk...
            store.clear_process_caches()
            with measure_symbolication() as cost:
                self.get(self.source_for(dif))
            self.assertEqual(cost.cache_storage_loads, 1)
            # ...and the next cold process maps it, without storage.
            store.clear_process_caches()
            with measure_symbolication() as cost:
                self.assertIsNotNone(self.get(self.source_for(dif)))
            self.assertEqual(cost.cache_disk_hits, 1)
            self.assertEqual(cost.files_opened, 0)

        # Unwritable: logged once, and symbolication carries on from storage.
        lift_log_disable(self)
        with tempfile.NamedTemporaryFile() as not_a_directory:
            unusable = f"{not_a_directory.name}/cache"
            with (
                override_settings(GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR=unusable),
                self.assertLogs("apps.symbolication.store", "WARNING") as logs,
            ):
                for _ in range(2):
                    store.clear_process_caches()
                    with measure_symbolication() as cost:
                        self.assertIsNotNone(self.get(self.source_for(dif)))
                    self.assertEqual(cost.cache_storage_loads, 1)
            self.assertEqual(len(logs.records), 1)


class MaintenanceTests(CacheTestCase):
    def cleanup(self):
        async_to_sync(cleanup_symbolication_caches)()

    def test_caches_of_deleted_sources_go(self):
        dif = self.upload_elf()
        self.get(self.source_for(dif))
        row = SymbolicationCache.objects.get()
        FileBlob.objects.filter(id=row.source_blob_id).delete()
        self.cleanup()
        self.assertFalse(SymbolicationCache.objects.exists())
        self.assertFalse(default_storage.exists(row.storage_name))

    def test_fileblob_deletes_null_the_link_in_the_database(self):
        """DB_SET_NULL: a raw delete, as code unaware of this model does,
        still works and unlinks the cache."""
        blob = store_blob("x", b"x")  # a chunk no File points at
        row = SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE, cache_key="k", format_tag="t", source_blob=blob
        )
        FileBlob.objects.filter(id=blob.id)._raw_delete("default")
        row.refresh_from_db()
        self.assertIsNone(row.source_blob_id)

    def test_other_formats_go_only_after_a_grace_period(self):
        blob = store_blob("x", b"x")
        name = "uploads/symbolcache/symcache/0.0/abc"
        default_storage.save(name, ContentFile(b"old cache"))
        old = SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE,
            cache_key="k",
            format_tag="0.0",
            source_blob=blob,
            state=CacheState.READY,
            storage_name=name,
        )
        self.cleanup()
        self.assertTrue(SymbolicationCache.objects.filter(id=old.id).exists())
        SymbolicationCache.objects.filter(id=old.id).update(
            updated=timezone.now() - maintenance.STALE_FORMAT_GRACE * 2
        )
        self.cleanup()
        self.assertFalse(SymbolicationCache.objects.filter(id=old.id).exists())
        self.assertFalse(default_storage.exists(name))

    def test_failed_builds_are_forgotten_after_days(self):
        row = self.row_for(self.upload_elf(), state=CacheState.FAILED)
        self.cleanup()
        self.assertTrue(SymbolicationCache.objects.filter(id=row.id).exists())
        SymbolicationCache.objects.filter(id=row.id).update(
            updated=timezone.now() - timedelta(days=30)
        )
        self.cleanup()
        self.assertFalse(SymbolicationCache.objects.filter(id=row.id).exists())

    def test_pages_through_every_row(self):
        blob = store_blob("x", b"x")
        SymbolicationCache.objects.bulk_create(
            [
                SymbolicationCache(
                    kind=CacheKind.SYMCACHE, cache_key=str(index), format_tag="0.0"
                )
                for index in range(7)
            ]
        )
        SymbolicationCache.objects.create(
            kind=CacheKind.SYMCACHE,
            cache_key="keep",
            format_tag=store.format_tag(CacheKind.SYMCACHE),
            source_blob=blob,
        )
        with mock.patch.object(maintenance, "PAGE_SIZE", 2):
            self.cleanup()
        self.assertEqual(
            list(SymbolicationCache.objects.values_list("cache_key", flat=True)),
            ["keep"],
        )


class EstimationOnceTests(CacheTestCase):
    def test_the_base_is_estimated_once_per_debug_file(self):
        """Chained exceptions share one estimate, instead of one each."""
        self.upload_elf()
        payload = native_crash()
        del payload["debug_meta"]
        first = payload["exception"]["values"][0]
        payload["exception"]["values"] = [first, first, first]
        with mock.patch(
            "apps.difs.stacktrace_processor._estimate_image_base",
            return_value=ELF_IMAGE_ADDR,
        ) as estimate:
            [event] = self.process(payload)
        estimate.assert_called_once()
        self.assertTrue(self.resolved(event))
