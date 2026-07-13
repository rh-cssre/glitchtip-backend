from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from glitchtip.url_validation import (
    check_url_safe,
    is_ip_blocked,
    validate_public_url,
)


class IsIPBlockedTests(SimpleTestCase):
    def test_loopback_blocked(self):
        self.assertTrue(is_ip_blocked("127.0.0.1"))
        self.assertTrue(is_ip_blocked("::1"))

    def test_rfc1918_blocked(self):
        self.assertTrue(is_ip_blocked("10.0.0.1"))
        self.assertTrue(is_ip_blocked("172.16.0.1"))
        self.assertTrue(is_ip_blocked("192.168.1.1"))

    def test_link_local_blocked(self):
        # Cloud instance metadata endpoint — the canonical SSRF target.
        self.assertTrue(is_ip_blocked("169.254.169.254"))

    def test_public_ip_allowed(self):
        self.assertFalse(is_ip_blocked("8.8.8.8"))
        self.assertFalse(is_ip_blocked("2606:4700:4700::1111"))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            is_ip_blocked("not-an-ip")


class ValidatePublicURLTests(SimpleTestCase):
    def test_blocks_loopback_literal(self):
        with self.assertRaises(ValueError):
            validate_public_url("http://127.0.0.1:6379/")

    def test_blocks_metadata_literal(self):
        with self.assertRaises(ValueError):
            validate_public_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_rfc1918_literal(self):
        with self.assertRaises(ValueError):
            validate_public_url("https://10.0.0.5/webhook")

    def test_allows_public_literal(self):
        validate_public_url("https://8.8.8.8/")  # no raise

    def test_allow_private_flag_bypasses(self):
        validate_public_url("http://127.0.0.1/", allow_private=True)

    def test_blocks_hostname_resolving_to_loopback(self):
        with mock.patch(
            "glitchtip.url_validation.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("127.0.0.1", 0))],
        ):
            with self.assertRaises(ValueError):
                validate_public_url("http://localhost.evil.example/")

    def test_dns_failure_is_permissive(self):
        # DNS failure defers to the runtime async check so that typos don't
        # leak information about internal name resolution.
        import socket

        with mock.patch(
            "glitchtip.url_validation.socket.getaddrinfo",
            side_effect=socket.gaierror,
        ):
            validate_public_url("http://no-such-host.invalid/")  # no raise


class CheckURLSafeAsyncTests(SimpleTestCase):
    def test_blocks_loopback_literal(self):
        self.assertFalse(async_to_sync(check_url_safe)("http://127.0.0.1/"))

    def test_allows_public_literal(self):
        self.assertTrue(async_to_sync(check_url_safe)("https://8.8.8.8/"))

    def test_allow_private_flag(self):
        self.assertTrue(
            async_to_sync(check_url_safe)("http://127.0.0.1/", allow_private=True)
        )

    def test_rebind_still_blocked_via_dns(self):
        async def fake_getaddrinfo(*_, **__):
            return [(0, 0, 0, "", ("10.0.0.1", 0))]

        with mock.patch(
            "asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo, create=True
        ):
            self.assertFalse(
                async_to_sync(check_url_safe)("http://public-name.example/")
            )


@override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=False)
class WebhookRecipientSchemaRejectsPrivateURL(SimpleTestCase):
    """Schema-level validation for alert recipient URLs.

    The pydantic field_validator hooks into create/update so operators see a
    400 at create time instead of a silent drop when the alert fires.
    """

    def _validate(self, payload):
        from apps.alerts.schema import ProjectAlertIn

        return ProjectAlertIn.model_validate(payload)

    def test_rejects_metadata_url(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self._validate(
                {
                    "name": "x",
                    "timespanMinutes": 1,
                    "quantity": 1,
                    "alertRecipients": [
                        {
                            "recipientType": "webhook",
                            "url": "http://169.254.169.254/latest/",
                        }
                    ],
                }
            )

    def test_accepts_public_url(self):
        self._validate(
            {
                "name": "x",
                "timespanMinutes": 1,
                "quantity": 1,
                "alertRecipients": [
                    {
                        "recipientType": "webhook",
                        "url": "https://example.com/hook",
                    }
                ],
            }
        )

    def test_rejects_loopback_for_zulip(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self._validate(
                {
                    "name": "x",
                    "timespanMinutes": 1,
                    "quantity": 1,
                    "alertRecipients": [
                        {
                            "recipientType": "zulip",
                            "url": "http://127.0.0.1/",
                            "botEmail": "b@x",
                            "apiKey": "k",
                            "channel": "c",
                        }
                    ],
                }
            )

    @override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=True)
    def test_allow_private_setting_enables_internal_url(self):
        self._validate(
            {
                "name": "x",
                "timespanMinutes": 1,
                "quantity": 1,
                "alertRecipients": [
                    {
                        "recipientType": "webhook",
                        "url": "http://127.0.0.1/internal-hook",
                    }
                ],
            }
        )


class WebhookRuntimeGuardTests(SimpleTestCase):
    """The send_* transports must refuse to fetch a blocked URL at runtime."""

    def test_send_webhook_blocked_url_does_not_call_aiohttp(self):
        from apps.alerts.webhooks import send_webhook

        with mock.patch("apps.alerts.webhooks.aiohttp.ClientSession") as mock_cs:
            result = async_to_sync(send_webhook)("http://127.0.0.1:6379/", "msg")
        self.assertIsNone(result)
        mock_cs.assert_not_called()

    def test_send_discord_blocked_url_does_not_call_aiohttp(self):
        from apps.alerts.webhooks import send_discord_webhook

        with mock.patch("apps.alerts.webhooks.aiohttp.ClientSession") as mock_cs:
            result = async_to_sync(send_discord_webhook)(
                "http://169.254.169.254/", "msg", []
            )
        self.assertIsNone(result)
        mock_cs.assert_not_called()

    @override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=True)
    def test_runtime_allow_private_setting_passes_through(self):
        from apps.alerts.webhooks import send_webhook

        mock_response = mock.AsyncMock()
        mock_response.__aenter__ = mock.AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = mock.AsyncMock(return_value=False)
        mock_session = mock.AsyncMock()
        mock_session.post = mock.MagicMock(return_value=mock_response)
        mock_session.__aenter__ = mock.AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = mock.AsyncMock(return_value=False)

        with mock.patch(
            "apps.alerts.webhooks.aiohttp.ClientSession",
            mock.MagicMock(return_value=mock_session),
        ):
            async_to_sync(send_webhook)("http://127.0.0.1/", "msg")

        mock_session.post.assert_called_once()
