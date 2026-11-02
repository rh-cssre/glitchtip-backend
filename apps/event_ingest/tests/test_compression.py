import json
from unittest import skipUnless

try:
    # Stdlib zstd (PEP 784) landed in Python 3.14. Runtime zstd decompression
    # is done in gt_rust, so there is no third-party zstd dependency to fall
    # back to on older Pythons — this test only needs a zstd *compressor* to
    # build the fixture body, which the 3.12 CI job simply skips.
    from compression import zstd
except ImportError:
    zstd = None

from django.tasks import task_backends
from django.urls import reverse

from .utils import EventIngestTestCase


class CompressionTestCase(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_store", args=[self.project.id]) + self.params
        self.event = self.get_json_data("events/test_data/py_hi_event.json")

    @skipUnless(zstd is not None, "stdlib compression.zstd requires Python 3.14+")
    def test_zstd_compression(self):
        json_data = json.dumps(self.event).encode("utf-8")
        compressed_data = zstd.compress(json_data)

        # TODO: re-add assertNumQueries once unit tests run on the async
        # backend. assertNumQueries only observes Django's sync connection and
        # can't count the ingest queries issued through async_connections.
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
