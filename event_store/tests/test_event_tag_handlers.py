from django.test import TestCase
from ..event_tag_processors import TAG_PROCESSORS


class EventTagTestCase(TestCase):
    def make_fake_request_with_ua(self, ua_string: str):
        return {"request": {"headers": [("User-Agent", ua_string)]}}

    def test_user_agent_processors(self):
        """
        Smoke test user agent processors
        No need to exhaustively test user-agents
        """
        ua_test_cases = [
            {
                "ua_string": "invalid string",
                "browser.name": "Other",
                "browser": "Other",
                "os.name": "Other",
                "device": None,
            },
            {
                "ua_string": "Mozilla/5.0 (Android 9; Mobile; rv:68.0) Gecko/68.0 Firefox/68.0",
                "browser.name": "Firefox Mobile",
                "browser": "Firefox Mobile 68.0",
                "os.name": "Android",
                "device": "Smartphone",
            },
        ]
        for ua_test_case in ua_test_cases:
            for Processor in TAG_PROCESSORS:
                processor = Processor()
                event = self.make_fake_request_with_ua(ua_test_case["ua_string"])
                self.assertEqual(
                    processor.get_tag_values(event),
                    ua_test_case[processor.tag],
                    str(processor),
                )

