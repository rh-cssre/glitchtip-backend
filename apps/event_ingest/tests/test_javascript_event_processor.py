import json
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from gt_rust.symbolic import SourceMapCache

from apps.event_ingest.javascript_event_processor import JavascriptEventProcessor
from apps.shared.schema.exception import StackTraceFrame

# Minified line 1 is ``a();b();``. The map sends generated column 0 (``a``) to
# original line 1 and generated column 4 (``b``) to original line 2, so a
# lookup that lands one column to the left of ``b`` resolves to the wrong line.
MINIFIED = b"a();b();\n"
SOURCEMAP = json.dumps(
    {
        "version": 3,
        "sources": ["src.js"],
        "sourcesContent": ["a();\nb();\n"],
        "names": [],
        "mappings": "AAAA,IACA",
    }
).encode()


class LookupTokenColumnTests(SimpleTestCase):
    """SDK frames carry 1-based ``lineno``/``colno``; the lookup must hit the
    exact generated column the frame names."""

    def lookup(self, colno: int):
        processor = JavascriptEventProcessor(release_id=0, data=None, debug_bundles=[])
        # Seed the event's cache memo, as the cache store would fill it.
        processor._sourcemap_caches[(1, 2)] = SourceMapCache.load(
            SourceMapCache.build(MINIFIED, SOURCEMAP)
        )
        frame = SimpleNamespace(abs_path="app.min.js", lineno=1, colno=colno)
        return async_to_sync(processor.lookup_token)(
            frame, SimpleNamespace(id=2), SimpleNamespace(id=1)
        )

    def test_column_resolves_to_its_own_token(self):
        token = self.lookup(colno=5)  # ``b`` is the 5th character
        self.assertEqual(token.line, 2)
        self.assertEqual(token.context_line.rstrip("\n"), "b();")

    def test_first_column_resolves_to_first_token(self):
        token = self.lookup(colno=1)
        self.assertEqual(token.line, 1)
        self.assertEqual(token.context_line.rstrip("\n"), "a();")


def build_cache(minified: bytes, sourcemap: dict) -> SourceMapCache:
    return SourceMapCache.load(
        SourceMapCache.build(minified, json.dumps(sourcemap).encode())
    )


class SourcemapShapeTests(SimpleTestCase):
    """Frames and maps of shapes that have broken symbolication before:
    each resolves, or is left alone, without an exception."""

    # Four minified lines, the last a long one, each mapped to its own line.
    FOUR_LINES = b"a();\nb();\nc();\n" + b"d();" * 10 + b"\n"
    FOUR_LINE_MAP = {
        "version": 3,
        "sources": ["src.js"],
        "sourcesContent": ["a();\nb();\nc();\nd();\n"],
        "names": [],
        "mappings": "AAAA;AACA;AACA;AACA,IAAA",
    }

    def resolve(self, cache, lineno, colno):
        processor = JavascriptEventProcessor(release_id=0, data=None, debug_bundles=[])
        processor._sourcemap_caches[(1, 2)] = cache
        frame = StackTraceFrame(
            abs_path="https://example.com/app.min.js",
            filename="https://example.com/app.min.js",
            function="?",
            lineno=lineno,
            colno=colno,
        )
        token = async_to_sync(processor.lookup_token)(
            frame, SimpleNamespace(id=2), SimpleNamespace(id=1)
        )
        if token is not None:
            processor.process_frame(frame, token)
        return token, frame

    def test_a_column_far_past_the_end_of_the_last_line(self):
        """A minified line's last token covers the rest of the line."""
        _, frame = self.resolve(
            build_cache(self.FOUR_LINES, self.FOUR_LINE_MAP), 4, 5411779
        )
        self.assertEqual((frame.filename, frame.lineno), ("src.js", 4))
        self.assertEqual(frame.context_line, "d();")

    def test_a_line_past_the_end_of_the_file(self):
        """No token: the frame is left as the SDK sent it."""
        token, frame = self.resolve(
            build_cache(self.FOUR_LINES, self.FOUR_LINE_MAP), 40, 1
        )
        self.assertIsNone(token)
        self.assertEqual((frame.lineno, frame.colno), (40, 1))
        self.assertIsNone(frame.context_line)

    def test_an_index_map_with_sections(self):
        index_map = {
            "version": 3,
            "sections": [
                {
                    "offset": {"line": 0, "column": 0},
                    "map": {
                        "version": 3,
                        "sources": ["one.js"],
                        "sourcesContent": ["one();\n"],
                        "names": [],
                        "mappings": "AAAA",
                    },
                },
                {
                    "offset": {"line": 1, "column": 0},
                    "map": {
                        "version": 3,
                        "sources": ["two.js"],
                        "sourcesContent": ["\n\ntwo();\n"],
                        "names": [],
                        "mappings": "AAEA",
                    },
                },
            ],
        }
        _, frame = self.resolve(build_cache(b"one();\ntwo();\n", index_map), 2, 1)
        self.assertEqual((frame.filename, frame.lineno), ("two.js", 3))
        self.assertEqual(frame.context_line, "two();")

    def test_a_map_without_sources_content(self):
        """The frame is mapped; there is just no source context."""
        without_content = json.loads(SOURCEMAP)
        del without_content["sourcesContent"]
        _, frame = self.resolve(build_cache(MINIFIED, without_content), 1, 5)
        self.assertEqual((frame.filename, frame.lineno), ("src.js", 2))
        self.assertIsNone(frame.context_line)
        self.assertIsNone(frame.pre_context)
