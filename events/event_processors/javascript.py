import itertools
from symbolic import SourceMapView
from sentry.utils.safe import get_path

from files.models import File
from .base import EventProcessorBase


class JavascriptEventProcessor(EventProcessorBase):
    def should_run(self, data):
        return data.get("platform") in ("javascript", "node") and self.release

    def get_stacktraces(self, data):
        exceptions = get_path(data, "exception", "values", filter=True, default=())
        stacktraces = [e["stacktrace"] for e in exceptions if e.get("stacktrace")]

        if "stacktrace" in data:
            stacktraces.append(data["stacktrace"])
        return stacktraces

    def get_valid_frames(self, stacktraces):
        frames = []
        frames = [stacktrace["frames"] for stacktrace in stacktraces]

        merged = list(itertools.chain(*frames))
        return [f for f in merged if f is not None and f.get("lineno") is not None]

    def transform(self, data):
        # Somewhat based on sentry/lang/javascript/processor.py
        stacktraces = self.get_stacktraces(data)
        frames = self.get_valid_frames(stacktraces)
        filenames = {frame["filename"].split("/")[-1] for frame in frames}
        files = File.objects.filter(
            releasefile__release=self.release,
            name__in={filename + ".map" for filename in filenames},
        )
        sourcemap_view = SourceMapView.from_json_bytes(
            files[0].blobs.first().blob.read()
        )
        token = sourcemap_view.lookup(
            frames[0]["lineno"] - 1,
            frames[0]["colno"] - 1,
            frames[0]["function"],
            # minified_source
        )
        import ipdb

        ipdb.set_trace()

        # TODO get stacktrace files names, filter only those release files
        return data
