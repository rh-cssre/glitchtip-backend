import json
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from gt_rust.symbolic import SourceMapCache

from apps.event_ingest.javascript_event_processor import JavascriptEventProcessor

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
