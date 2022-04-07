import itertools
from symbolic import SourceMapView, SourceView
from sentry.utils.safe import get_path

from files.models import File
from .base import EventProcessorBase


class JavascriptEventProcessor(EventProcessorBase):
    release_files = None
    # Somewhat based on sentry/lang/javascript/processor.py
    def should_run(self):
        return self.data.get("platform") in ("javascript", "node") and self.release

    def get_stacktraces(self):
        exceptions = get_path(self.data, "exception", "values", filter=True, default=())
        stacktraces = [e["stacktrace"] for e in exceptions if e.get("stacktrace")]

        if "stacktrace" in self.data:
            stacktraces.append(self.data["stacktrace"])
        return stacktraces

    def get_valid_frames(self, stacktraces):
        frames = []
        frames = [stacktrace["frames"] for stacktrace in stacktraces]

        merged = list(itertools.chain(*frames))
        return [f for f in merged if f is not None and f.get("lineno") is not None]

    def process_frame(self, frame, map_file, minified_source):
        # Required to determine source
        if not frame.get("abs_path") or not frame.get("lineno"):
            return

        sourcemap_view = SourceMapView.from_json_bytes(
            map_file.blobs.first().blob.read()
        )
        minified_source_view = SourceView.from_bytes(
            minified_source.blobs.first().blob.read()
        )
        token = sourcemap_view.lookup(
            frame["lineno"] - 1,
            frame["colno"] - 1,
            frame["function"],
            minified_source_view,
        )

    def transform(self):
        stacktraces = self.get_stacktraces()
        frames = self.get_valid_frames(stacktraces)
        filenames = {frame["filename"].split("/")[-1] for frame in frames}
        # Make a guess at which files are relevant, match then better after
        source_files = File.objects.filter(
            releasefile__release=self.release,
            name__in={filename + ".map" for filename in filenames} | filenames,
        )

        frames_with_source = []
        for frame in frames:
            minified_filename = frame["abs_path"].split("/")[-1]
            map_filename = minified_filename + ".map"
            minified_file = None
            map_file = None
            for source_file in source_files:
                if source_file.name == minified_filename:
                    minified_file = source_file
                if source_file.name == map_filename:
                    map_file = source_file
            if map_file:
                frames_with_source.append((frame, map_file, minified_file))

        for frame_with_source in frames_with_source:
            self.process_frame(*frame_with_source)

        # TODO get stacktrace files names, filter only those release files
