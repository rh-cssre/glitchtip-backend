from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase

from apps.event_ingest.api import get_ip_address


class GetIpAddressTest(SimpleTestCase):
    """Pin the client-IP rules the Rust envelope path mirrors, so the
    Python ingest paths resolve the same IP for the same request."""

    def _ip(self, **meta):
        request = RequestFactory().post("/", **meta)
        request.auth = SimpleNamespace(should_scrub_ip_addresses=False)
        return get_ip_address(request)

    def test_remote_addr(self):
        self.assertEqual(self._ip(REMOTE_ADDR="93.184.216.34"), "93.184.216.34")

    def test_private_leading_forwarded_hop_is_not_skipped(self):
        self.assertIsNone(
            self._ip(
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR="10.0.0.5, 93.184.216.34",
            )
        )

    def test_cdn_client_ip_header_is_not_trusted(self):
        self.assertEqual(
            self._ip(REMOTE_ADDR="93.184.216.34", HTTP_TRUE_CLIENT_IP="8.8.8.8"),
            "93.184.216.34",
        )
