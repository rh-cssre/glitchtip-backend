from django.test import TestCase
from ..event_context_processors import (
    EVENT_CONTEXT_PROCESSORS,
    UserAgentContextProcessor,
)


class EventTagTestCase(TestCase):
    def make_fake_event_with_ua(self, ua_string: str):
        return {"request": {"headers": [("User-Agent", ua_string)]}}

    def test_user_agent_context_processors(self):
        """
        Smoke test user agent context processors
        No need to exhaustively test user-agents
        """
        ua_test_cases = [
            {
                "ua_string": "invalid string",
                "browser": {"name": "Other"},
                "os": {"name": "Other"},
                "device": {"family": "Other"},
            },
            {
                "ua_string": "Mozilla/5.0 (Android 9; Mobile; rv:68.0) Gecko/68.0 Firefox/68.0",
                "browser": {"name": "Firefox Mobile", "version": "68.0"},
                "os": {"name": "Android", "version": "9"},
                "device": {
                    "brand": "Generic",
                    "family": "Generic Smartphone",
                    "model": "Smartphone",
                },
            },
        ]
        for ua_test_case in ua_test_cases:
            for Processor in EVENT_CONTEXT_PROCESSORS:
                processor = Processor()
                event = self.make_fake_event_with_ua(ua_test_case["ua_string"])
                context = processor.get_context(event)
                self.assertEqual(
                    context, ua_test_case[processor.name], str(processor),
                )

    def test_user_agent_context_processor_no_ua(self):
        """ Missing UA header should return None """
        event = {
            "request": {
                "headers": [
                    ["Accept-Encoding", "gzip"],
                    ["Connection", "Keep-Alive"],
                    ["Content-Length", "123"],
                    ["Content-Type", "multipart/form-data; boundary=1M",],
                    ["Host", "example.com"],
                ]
            }
        }

        self.assertFalse(UserAgentContextProcessor().get_context(event))
        self.assertFalse(UserAgentContextProcessor().get_context({}))
        self.assertFalse(UserAgentContextProcessor().get_context({"request": {}}))
