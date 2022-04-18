import copy
import itertools
import re
from os.path import splitext
from urllib.parse import urlsplit

from symbolic import SourceMapView, SourceView

from files.models import File
from sentry.utils.safe import get_path

from .base import EventProcessorBase

UNKNOWN_MODULE = "<unknown module>"
CLEAN_MODULE_RE = re.compile(
    r"""^
(?:/|  # Leading slashes
(?:
    (?:java)?scripts?|js|build|static|node_modules|bower_components|[_\.~].*?|  # common folder prefixes
    v?(?:\d+\.)*\d+|   # version numbers, v1, 1.0.0
    [a-f0-9]{7,8}|     # short sha
    [a-f0-9]{32}|      # md5
    [a-f0-9]{40}       # sha1
)/)+|
(?:[-\.][a-f0-9]{7,}$)  # Ending in a commitish
""",
    re.X | re.I,
)
VERSION_RE = re.compile(r"^[a-f0-9]{32}|[a-f0-9]{40}$", re.I)
NODE_MODULES_RE = re.compile(r"\bnode_modules/")


def generate_module(src):
    """
    Converts a url into a made-up module name by doing the following:
     * Extract just the path name ignoring querystrings
     * Trimming off the initial /
     * Trimming off the file extension
     * Removes off useless folder prefixes

    e.g. http://google.com/js/v1.0/foo/bar/baz.js -> foo/bar/baz
    """
    if not src:
        return UNKNOWN_MODULE

    filename, _ = splitext(urlsplit(src).path)
    if filename.endswith(".min"):
        filename = filename[:-4]

    tokens = filename.split("/")
    for idx, token in enumerate(tokens):
        # a SHA
        if VERSION_RE.match(token):
            return "/".join(tokens[idx + 1 :])

    return CLEAN_MODULE_RE.sub("", filename) or UNKNOWN_MODULE


class JavascriptEventProcessor(EventProcessorBase):
    """
    Based partially on sentry/lang/javascript/processor.py
    """

    release_files = None

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

        minified_source.blob.blob.seek(0)
        map_file.blob.blob.seek(0)
        sourcemap_view = SourceMapView.from_json_bytes(map_file.blob.blob.read())
        minified_source_view = SourceView.from_bytes(minified_source.blob.blob.read())
        token = sourcemap_view.lookup(
            frame["lineno"] - 1,
            frame["colno"] - 1,
            frame["function"],
            minified_source_view,
        )

        if not token:
            return
        frame["lineno"] = token.src_line + 1
        frame["colno"] = token.src_col + 1
        if token.function_name:
            frame["function"] = token.function_name

        filename = token.src
        abs_path = frame["abs_path"]
        in_app = None
        # special case webpack support
        # abs_path will always be the full path with webpack:/// prefix.
        # filename will be relative to that
        if abs_path.startswith("webpack:"):
            filename = abs_path
            # webpack seems to use ~ to imply "relative to resolver root"
            # which is generally seen for third party deps
            # (i.e. node_modules)
            if "/~/" in filename:
                filename = "~/" + abs_path.split("/~/", 1)[-1]
            else:
                filename = filename.split("webpack:///", 1)[-1]

            # As noted above:
            # * [js/node] '~/' means they're coming from node_modules, so these are not app dependencies
            # * [node] sames goes for `./node_modules/` and '../node_modules/', which is used when bundling node apps
            # * [node] and webpack, which includes it's own code to bootstrap all modules and its internals
            #   eg. webpack:///webpack/bootstrap, webpack:///external
            if (
                filename.startswith("~/")
                or "/node_modules/" in filename
                or not filename.startswith("./")
            ):
                in_app = False
            # And conversely, local dependencies start with './'
            elif filename.startswith("./"):
                in_app = True
            # We want to explicitly generate a webpack module name
            frame["module"] = generate_module(filename)
        elif "/node_modules/" in abs_path:
            in_app = False

        if abs_path.startswith("app:"):
            if filename and NODE_MODULES_RE.search(filename):
                in_app = False
            else:
                in_app = True

        frame["filename"] = filename
        if not frame.get("module") and abs_path.startswith(
            ("http:", "https:", "webpack:", "app:")
        ):
            frame["module"] = generate_module(abs_path)
        if in_app is not None:
            frame["in_app"] = in_app

    def transform(self):
        stacktraces = self.get_stacktraces()
        frames = self.get_valid_frames(stacktraces)
        filenames = {frame["filename"].split("/")[-1] for frame in frames}
        # Make a guess at which files are relevant, match then better after
        source_files = File.objects.filter(
            releasefile__release=self.release,
            name__in={filename + ".map" for filename in filenames} | filenames,
        )

        if not source_files:
            return

        # Copy original stacktrace before modifying them
        for exception in get_path(
            self.data, "exception", "values", filter=True, default=()
        ):
            exception["raw_stacktrace"] = copy.deepcopy(exception["stacktrace"])

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
