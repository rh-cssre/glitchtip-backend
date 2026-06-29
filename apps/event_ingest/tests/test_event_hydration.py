from apps.event_ingest.tests.utils import EventIngestTestCase
from apps.issue_events.models import IssueEvent


class EventHydrationTestCase(EventIngestTestCase):
    def test_swift_exception_hydration(self):
        """
        Test Case A (Swift): Payload with exception (missing stacktrace) and a thread (with stacktrace).
        Assert that after processing, the exception object contains the stacktrace.
        """
        payload = {
            "exception": {
                "values": [
                    {
                        "type": "NSError",
                        "value": "An error occurred",
                        "thread_id": "0",  # String to match schema usually
                    }
                ]
            },
            "threads": {
                "values": [
                    {
                        "id": "0",
                        "current": True,
                        "crashed": False,
                        "stacktrace": {
                            "frames": [
                                {
                                    "function": "main",
                                    "in_app": True,
                                    "filename": "main.swift",
                                }
                            ]
                        },
                    }
                ]
            },
            "platform": "cocoa",
        }

        self.process_events(payload)

        event = IssueEvent.objects.first()
        self.assertIsNotNone(event)

        exception = event.data["exception"]["values"][0]
        self.assertIn("stacktrace", exception)
        self.assertIsNotNone(exception["stacktrace"])
        self.assertEqual(len(exception["stacktrace"]["frames"]), 1)
        self.assertEqual(exception["stacktrace"]["frames"][0]["function"], "main")

    def test_csharp_logentry_no_exception_hydration(self):
        """
        Test Case B (C#): Payload with logentry and threads, but no exception.
        Assert that after processing, the event still has no exception interface.
        """
        payload = {
            "logentry": {"message": "Just a log"},
            "threads": {
                "values": [{"id": 1, "current": True, "stacktrace": {"frames": []}}]
            },
            "platform": "csharp",
        }

        self.process_events(payload)

        event = IssueEvent.objects.first()
        self.assertIsNotNone(event)

        self.assertNotIn("exception", event.data)
        # Or if it's there it should be None or empty
        if "exception" in event.data and event.data["exception"]:
            self.assertIsNone(event.data["exception"])

    def test_hydration_priority_thread_id(self):
        """
        Test that we match by thread_id first.
        """
        payload = {
            "exception": {
                "values": [
                    {"type": "Error", "value": "Thread 2 error", "thread_id": "2"}
                ]
            },
            "threads": {
                "values": [
                    {
                        "id": "1",
                        "current": True,
                        "stacktrace": {
                            "frames": [{"function": "thread1", "filename": "t1.c"}]
                        },
                    },
                    {
                        "id": "2",
                        "current": False,
                        "stacktrace": {
                            "frames": [{"function": "thread2", "filename": "t2.c"}]
                        },
                    },
                ]
            },
            "platform": "native",
        }

        self.process_events(payload)
        event = IssueEvent.objects.first()
        exception = event.data["exception"]["values"][0]
        self.assertEqual(exception["stacktrace"]["frames"][0]["function"], "thread2")

    def test_hydration_priority_current_thread(self):
        """
        Test that we match by current=True if thread_id matching fails (or exception has no thread_id).
        """
        payload = {
            "exception": {
                "values": [
                    {
                        "type": "Error",
                        "value": "Current thread error",
                        # No thread_id
                    }
                ]
            },
            "threads": {
                "values": [
                    {
                        "id": "1",
                        "current": True,
                        "stacktrace": {
                            "frames": [{"function": "thread1", "filename": "t1.c"}]
                        },
                    },
                    {
                        "id": "2",
                        "current": False,
                        "stacktrace": {
                            "frames": [{"function": "thread2", "filename": "t2.c"}]
                        },
                    },
                ]
            },
            "platform": "native",
        }

        self.process_events(payload)
        event = IssueEvent.objects.first()
        exception = event.data["exception"]["values"][0]
        self.assertEqual(exception["stacktrace"]["frames"][0]["function"], "thread1")
