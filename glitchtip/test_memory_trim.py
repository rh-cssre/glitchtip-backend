import asyncio
from unittest import mock

from django.test import SimpleTestCase, override_settings

from glitchtip.memory_trim import PeriodicMemoryTrim


class MockASGIApp:
    def __init__(self):
        self.calls = 0
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope


class PeriodicMemoryTrimTestCase(SimpleTestCase):
    async def test_passthrough(self):
        app = MockASGIApp()
        wrapper = PeriodicMemoryTrim(app, interval=0)
        scope = {"type": "http", "path": "/"}
        await wrapper(scope, None, None)
        self.assertEqual(app.calls, 1)
        self.assertIs(app.scope, scope)

    async def test_disabled_interval_starts_no_timer(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=0)
        await wrapper({"type": "http"}, None, None)
        self.assertIsNone(wrapper._task)

    async def test_timer_trims_periodically(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=0.01)
        with mock.patch("glitchtip.memory_trim.malloc_trim") as trim:
            await wrapper({"type": "http"}, None, None)
            self.assertIsNotNone(wrapper._task)
            self.addCleanup(wrapper._task.cancel)
            await asyncio.sleep(0.1)
            self.assertGreater(trim.call_count, 0)

    async def test_timer_started_once(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=60)
        await wrapper({"type": "http"}, None, None)
        task = wrapper._task
        self.addCleanup(task.cancel)
        await wrapper({"type": "http"}, None, None)
        self.assertIs(wrapper._task, task)

    async def test_lifespan_shutdown_cancels_timer(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=60)

        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

        async def receive():
            return messages.pop(0)

        received = []

        class DrainingApp:
            async def __call__(self, scope, wrapped_receive, send):
                received.append(await wrapped_receive())
                received.append(await wrapped_receive())

        wrapper.app = DrainingApp()
        await wrapper({"type": "lifespan"}, receive, None)
        # Messages pass through unchanged and the timer is cancelled.
        self.assertEqual(
            [m["type"] for m in received],
            ["lifespan.startup", "lifespan.shutdown"],
        )
        await asyncio.sleep(0)
        self.assertTrue(wrapper._task.cancelled())

    @override_settings(GLITCHTIP_MALLOC_TRIM_INTERVAL=120)
    def test_settings_interval(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp())
        self.assertEqual(wrapper.interval, 120)
