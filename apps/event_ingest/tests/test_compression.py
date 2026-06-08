import json

try:
    from compression import zstd
except ImportError:
    try:
        import zstandard as zstd
    except ImportError:
        import zstd

from django.tasks import task_backends
from django.urls import reverse

from .utils import EventIngestTestCase


class CompressionTestCase(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_store", args=[self.project.id]) + self.params
        self.event = self.get_json_data("events/test_data/py_hi_event.json")

    def test_zstd_compression(self):
        json_data = json.dumps(self.event).encode("utf-8")
        compressed_data = zstd.compress(json_data)

        with self.assertNumQueries(19):
            res = self.client.post(
                self.url,
                compressed_data,
                content_type="application/json",
                HTTP_CONTENT_ENCODING="zstd",
            )
            task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.event["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
