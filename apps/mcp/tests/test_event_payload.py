"""Tests for budget-aware, drill-down event serialization (apps/mcp/event_payload).

Event field contents are untrusted, DSN-submitted data; these tests build
synthetic events to exercise the size-shedding and drill-down logic.
"""

import json

from asgiref.sync import async_to_sync
from django.test import TestCase
from model_bakery import baker

from apps.mcp.data import get_event
from apps.mcp.event_payload import (
    CHARS_PER_TOKEN,
    DEFAULT_EVENT_TOKEN_BUDGET,
    MAX_BREADCRUMBS_DEFAULT,
    serialize_event_detail,
    serialize_event_lean,
)
from glitchtip.test_utils.issue import amake_issue


def _frames_of(result: dict) -> list:
    for entry in result.get("entries", []):
        if entry.get("type") == "exception":
            values = entry["data"]["values"]
            frames = []
            for value in values:
                stacktrace = value.get("stacktrace") or {}
                frames.extend(stacktrace.get("frames", []))
            return frames
    return []


def _breadcrumbs_of(result: dict) -> list:
    for entry in result.get("entries", []):
        if entry.get("type") == "breadcrumbs":
            return entry["data"]["values"]
    return []


def _make_event_data(
    n_frames: int = 3,
    n_system_frames: int = 0,
    include_builtins: bool = True,
    n_breadcrumbs: int = 0,
    breadcrumb_data_size: int = 0,
    request_body: str | None = None,
    huge_field: int = 0,
) -> dict:
    frames = []
    for i in range(n_frames):
        frames.append(
            {
                "filename": f"app/module_{i}.py",
                "function": f"handler_{i}",
                "module": f"app.module_{i}",
                "lineno": 10 + i,
                "colno": 4,
                "in_app": True,
                "context_line": f"    do_thing_{i}()",
                "vars": {
                    "local_x": str(i),
                    "request": "<Request object>",
                    **(
                        {"__builtins__": {"abs": "<builtin>", "len": "<builtin>"}}
                        if include_builtins
                        else {}
                    ),
                },
            }
        )
    for i in range(n_system_frames):
        frames.append(
            {
                "filename": f"/usr/lib/python/site-packages/dep_{i}.py",
                "function": f"internal_{i}",
                "module": f"dep.{i}",
                "lineno": 100 + i,
                "in_app": False,
                "context_line": "    call_next()",
                "vars": {"y": str(i)},
            }
        )

    data: dict = {
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": "something went wrong",
                    "stacktrace": {"frames": frames},
                }
            ]
        }
    }

    if n_breadcrumbs:
        crumbs = []
        blob = "x" * breadcrumb_data_size if breadcrumb_data_size else None
        for i in range(n_breadcrumbs):
            crumb = {
                "category": "sql",
                "level": "info",
                "message": f"breadcrumb {i}",
                "timestamp": "2025-01-01T00:00:00Z",
            }
            if blob:
                crumb["data"] = {"sql": f"SELECT * FROM t WHERE id={i} -- {blob}"}
            crumbs.append(crumb)
        data["breadcrumbs"] = {"values": crumbs}

    if request_body is not None:
        data["request"] = {
            "url": "https://example.com/api",
            "method": "POST",
            "headers": [["Content-Type", "application/json"]],
            "data": request_body,
        }

    if huge_field:
        data["contexts"] = {"custom": {"blob": "z" * huge_field}}

    return data


class LeanSerializationTest(TestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization
        self.issue = baker.make("issue_events.Issue", project=self.project)

    def _make_event(self, data):
        return baker.make(
            "issue_events.IssueEvent",
            issue=self.issue,
            organization=self.organization,
            data=data,
            tags={"server_name": "pod-1"},
        )

    def test_vars_and_builtins_stripped_by_default(self):
        event = self._make_event(_make_event_data(n_frames=3, include_builtins=True))
        result = serialize_event_lean(event)

        frames = _frames_of(result)
        self.assertEqual(len(frames), 3)
        for frame in frames:
            self.assertNotIn("vars", frame)
            self.assertTrue(frame.get("varsOmitted"))
            self.assertIn("frameIndex", frame)

        # __builtins__ must not appear anywhere in the serialized output.
        dumped = json.dumps(result)
        self.assertNotIn("__builtins__", dumped)
        self.assertNotIn("<builtin>", dumped)

        omissions = result["_meta"]["omissions"]
        self.assertEqual(omissions["frameVars"]["framesWithVars"], 3)

    def test_no_vars_no_marker(self):
        data = _make_event_data(n_frames=2, include_builtins=False)
        for value in data["exception"]["values"]:
            for frame in value["stacktrace"]["frames"]:
                frame.pop("vars", None)
        event = self._make_event(data)
        result = serialize_event_lean(event)
        self.assertNotIn("frameVars", result["_meta"]["omissions"])
        for frame in _frames_of(result):
            self.assertNotIn("varsOmitted", frame)

    def test_request_body_stripped(self):
        event = self._make_event(
            _make_event_data(n_frames=1, request_body="BODY" * 100)
        )
        result = serialize_event_lean(event)
        self.assertIn("requestBody", result["_meta"]["omissions"])
        dumped = json.dumps(result)
        self.assertNotIn("BODYBODY", dumped)

    def test_budget_enforced_on_huge_event(self):
        # Many frames, many fat breadcrumbs, a huge single field — every
        # dimension independently oversized.
        event = self._make_event(
            _make_event_data(
                n_frames=50,
                n_system_frames=400,
                n_breadcrumbs=5000,
                breadcrumb_data_size=200,
                request_body="B" * 50_000,
                huge_field=200_000,
            )
        )
        result = serialize_event_lean(event)

        char_budget = DEFAULT_EVENT_TOKEN_BUDGET * CHARS_PER_TOKEN
        measured = len(json.dumps(result))
        self.assertLessEqual(
            measured,
            char_budget,
            f"payload {measured} chars exceeds budget {char_budget}",
        )
        self.assertLessEqual(
            result["_meta"]["estimatedTokens"], DEFAULT_EVENT_TOKEN_BUDGET
        )

        # The exception core survives.
        exc = next(e for e in result["entries"] if e["type"] == "exception")
        value = exc["data"]["values"][0]
        self.assertEqual(value["type"], "ValueError")
        self.assertEqual(value["value"], "something went wrong")

        omissions = result["_meta"]["omissions"]
        self.assertTrue(result["_meta"]["truncated"])
        # Frames and breadcrumbs were both shed.
        self.assertIn("frames", omissions)
        self.assertIn("breadcrumbs", omissions)

    def test_breadcrumbs_capped_with_accurate_marker(self):
        n = 100
        event = self._make_event(
            _make_event_data(n_frames=1, n_breadcrumbs=n, breadcrumb_data_size=2000)
        )
        result = serialize_event_lean(event)
        shown = _breadcrumbs_of(result)
        self.assertLessEqual(len(shown), MAX_BREADCRUMBS_DEFAULT)

        bc = result["_meta"]["omissions"]["breadcrumbs"]
        self.assertEqual(bc["total"], n)
        self.assertEqual(bc["shown"] + bc["omitted"], n)
        # Most-recent kept: last breadcrumb present, first dropped.
        messages = [c.get("message") for c in shown]
        self.assertIn(f"breadcrumb {n - 1}", messages)
        self.assertNotIn("breadcrumb 0", messages)

    def test_system_frames_shed_but_in_app_kept(self):
        event = self._make_event(
            _make_event_data(
                n_frames=5,
                n_system_frames=500,
                include_builtins=False,
                huge_field=0,
            )
        )
        result = serialize_event_lean(event)
        frames = _frames_of(result)
        # All kept frames after system-shedding should be in-app.
        in_app_flags = [f.get("inApp") for f in frames]
        self.assertTrue(all(flag is True for flag in in_app_flags))
        self.assertIn("frames", result["_meta"]["omissions"])


class DrillDownTest(TestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization
        self.issue = baker.make("issue_events.Issue", project=self.project)

    def _make_event(self, data):
        return baker.make(
            "issue_events.IssueEvent",
            issue=self.issue,
            organization=self.organization,
            data=data,
            tags={},
        )

    def test_drill_down_vars_returns_specific_frame(self):
        event = self._make_event(_make_event_data(n_frames=3, include_builtins=True))
        result = serialize_event_detail(event, "vars", frame=1)
        self.assertEqual(result["frame"], 1)
        self.assertEqual(result["vars"]["local_x"], "1")
        # __builtins__ dropped even in drill-down.
        self.assertNotIn("__builtins__", result["vars"])
        self.assertIn("builtins", result["_meta"]["omissions"])
        self.assertEqual(result["frameSummary"]["function"], "handler_1")

    def test_drill_down_vars_requires_frame(self):
        event = self._make_event(_make_event_data(n_frames=2))
        with self.assertRaises(ValueError):
            serialize_event_detail(event, "vars")

    def test_drill_down_vars_out_of_range(self):
        event = self._make_event(_make_event_data(n_frames=2))
        with self.assertRaises(ValueError):
            serialize_event_detail(event, "vars", frame=99)

    def test_drill_down_frames_pagination(self):
        event = self._make_event(
            _make_event_data(n_frames=10, n_system_frames=10, include_builtins=False)
        )
        result = serialize_event_detail(event, "frames", offset=5, limit=4)
        self.assertEqual(result["total"], 20)
        self.assertEqual(result["offset"], 5)
        self.assertEqual(result["shown"], 4)
        indices = [f["frameIndex"] for f in result["frames"]]
        self.assertEqual(indices, [5, 6, 7, 8])
        self.assertTrue(all("hasVars" in f for f in result["frames"]))

    def test_drill_down_breadcrumbs_pagination_includes_data(self):
        event = self._make_event(
            _make_event_data(n_frames=1, n_breadcrumbs=100, breadcrumb_data_size=10)
        )
        result = serialize_event_detail(event, "breadcrumbs", offset=0, limit=5)
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["shown"], 5)
        # offset 0 is the oldest breadcrumb, and data is included here.
        self.assertEqual(result["breadcrumbs"][0]["message"], "breadcrumb 0")
        self.assertIn("data", result["breadcrumbs"][0])

    def test_drill_down_request_returns_body(self):
        event = self._make_event(
            _make_event_data(n_frames=1, request_body="secret-payload")
        )
        result = serialize_event_detail(event, "request")
        self.assertEqual(result["request"]["data"], "secret-payload")

    def test_drill_down_unknown_section(self):
        event = self._make_event(_make_event_data(n_frames=1))
        with self.assertRaises(ValueError):
            serialize_event_detail(event, "nonsense")

    def test_drill_down_vars_budget_bounded(self):
        data = _make_event_data(n_frames=1, include_builtins=False)
        # A single frame with an enormous local var.
        data["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"] = {
            "blob": "q" * 500_000
        }
        event = self._make_event(data)
        result = serialize_event_detail(event, "vars", frame=0)
        from apps.mcp.event_payload import CHARS_PER_TOKEN, DRILL_DOWN_TOKEN_BUDGET

        self.assertLessEqual(
            len(json.dumps(result)),
            DRILL_DOWN_TOKEN_BUDGET * CHARS_PER_TOKEN + 500,
        )
        self.assertIn("_meta", result)


class AsyncPathTest(TestCase):
    """The lean + drill-down serializers must run on the async fetch path
    without triggering SynchronousOnlyOperation (no lazy sync ORM access)."""

    def setUp(self):
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization
        self.user = baker.make("users.user")
        self.organization.add_user(self.user)

    async def _amake_event(self):
        issue = await amake_issue(project=self.project)
        return await baker.amake(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            data=_make_event_data(n_frames=3, n_breadcrumbs=30, request_body="body"),
            tags={"env": "prod"},
        )

    async def test_lean_serialization_async_no_sync_orm(self):
        event = await self._amake_event()
        fetched = await get_event(self.user.id, str(event.id))
        self.assertIsNotNone(fetched)
        # Runs inside the async test's event loop; a lazy sync ORM query here
        # would raise SynchronousOnlyOperation.
        result = serialize_event_lean(fetched)
        self.assertIn("_meta", result)
        detail = serialize_event_detail(fetched, "frames", limit=2)
        self.assertEqual(detail["section"], "frames")

    def test_sync_wrapper(self):
        # Sanity: the async coroutine test above also works via async_to_sync.
        event = async_to_sync(self._amake_event)()
        result = serialize_event_lean(event)
        self.assertTrue(result["_meta"]["lean"])
