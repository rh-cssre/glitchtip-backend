from django.test import SimpleTestCase
from pydantic import ValidationError

from apps.event_ingest.schema import WebIngestIssueEvent


class LogEntryTestCase(SimpleTestCase):
    def test_logentry_params_with_null(self):
        """
        Verify that logentry params can contain null values in both list and dict formats.
        This matches the reported ValidationError.
        """
        data = {
            "event_id": "ff761be5f2734d4ba80240612c806460",
            "timestamp": "2026-01-23T18:36:09.757729Z",
            "logentry": {
                "message": "FTL exception for locale [%s], message '%s', args %r: %s",
                "params": [
                    "en",
                    "template-type-ai-quiz",
                    None,
                    "KeyError('template-type-ai-quiz')",
                ],
            },
        }
        try:
            event = WebIngestIssueEvent(**data)
            self.assertEqual(event.logentry.params[2], None)
        except ValidationError as e:
            self.fail(
                f"WebIngestIssueEvent raised ValidationError with null params: {e}"
            )

    def test_logentry_params_dict_with_null(self):
        data = {"logentry": {"message": "Hello {name}", "params": {"name": None}}}
        event = WebIngestIssueEvent(**data)
        self.assertEqual(event.logentry.params["name"], None)

    def test_tags_coercion_in_list(self):
        """
        Verify that tags in list format are coerced to strings if they are bools.
        """
        data = {"tags": [["is_test", True], ["version", 1.0]]}
        event = WebIngestIssueEvent(**data)
        # Note: WebIngestIssueEvent has a validator 'prefer_dict' for tags
        self.assertEqual(event.tags["is_test"], "True")
        self.assertEqual(event.tags["version"], "1.0")
