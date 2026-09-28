import copy
import logging
import re
from os.path import splitext
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from gt_rust.symbolic import SourceMapCache

from apps.sourcecode.models import DebugSymbolBundle
from apps.symbolication.models import CacheKind
from apps.symbolication.store import CacheSource, get_cache

if TYPE_CHECKING:
    from apps.shared.schema.exception import EventException, StackTraceFrame

    from .schema import IssueEventSchema

logger = logging.getLogger(__name__)

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
    re.VERBOSE | re.IGNORECASE,
)
VERSION_RE = re.compile(r"^[a-f0-9]{32}|[a-f0-9]{40}$", re.IGNORECASE)
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
    filename = filename.removesuffix(".min")

    tokens = filename.split("/")
    for idx, token in enumerate(tokens):
        # a SHA
        if VERSION_RE.match(token):
            return "/".join(tokens[idx + 1 :])

    return CLEAN_MODULE_RE.sub("", filename) or UNKNOWN_MODULE


class JavascriptEventProcessor:
    """
    Based partially on sentry/lang/javascript/processor.py
    """

    def __init__(
        self,
        release_id: int,
        data: "IssueEventSchema",
        debug_bundles: list[DebugSymbolBundle],
    ):
        self.release_id = release_id
        self.data = data
        self.debug_bundles = debug_bundles
        # Parsed sourcemaps memoized for the lifetime of this processor, i.e.
        # for one event. A new processor is created per event, so this never
        # outlives the event and needs no eviction or staleness handling.
        self._sourcemap_caches: dict[tuple[int, int], SourceMapCache | None] = {}

    def get_stacktrace_exceptions(self) -> list["EventException"]:
        data = self.data
        if data.exception:
            return [
                exception for exception in data.exception.values if exception.stacktrace
            ]
        return []

    def get_valid_frames(self, exception: "EventException") -> list["StackTraceFrame"]:
        stacktrace = exception.stacktrace
        if not stacktrace:
            return []
        return [
            frame
            for frame in stacktrace.frames
            if frame is not None and frame.lineno is not None
        ]

    async def get_sourcemap_cache(self, minified_source, map_file):
        """The stored lookup cache for this minified file and sourcemap, or
        None while it is unavailable. Memoized for the event, so frames that
        share a bundle ask the cache store once."""
        key = (minified_source.id, map_file.id)
        if key in self._sourcemap_caches:
            return self._sourcemap_caches[key]
        cache = None
        if minified_source.blob_id and map_file.blob_id:
            cache = await get_cache(
                CacheSource(
                    kind=CacheKind.SOURCEMAP,
                    cache_key=f"{minified_source.blob.checksum}:{map_file.blob.checksum}",
                    source_blob_id=minified_source.blob_id,
                    secondary_blob_id=map_file.blob_id,
                )
            )
        self._sourcemap_caches[key] = cache
        return cache

    async def lookup_token(self, frame, map_file, minified_source):
        # Required to determine source
        if not frame.abs_path or not frame.lineno or not frame.colno:
            return None

        cache = await self.get_sourcemap_cache(minified_source, map_file)
        if cache is None:
            return None
        # Both positions are 1-based, as the SDK sends them; the lookup
        # converts to 0-based itself.
        return cache.lookup(
            frame.lineno,
            frame.colno,
            5,  # context_lines
        )

    def process_frame(self, frame, token):
        frame.lineno = token.line
        frame.colno = token.col
        if token.function_name:
            frame.function = token.function_name

        filename = token.src
        abs_path = frame.abs_path
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
            frame.module = generate_module(filename)
        elif "/node_modules/" in abs_path:
            in_app = False

        if abs_path.startswith("app:"):
            if filename and NODE_MODULES_RE.search(filename):
                in_app = False
            else:
                in_app = True

        frame.filename = filename
        if not frame.module and abs_path.startswith(
            ("http:", "https:", "webpack:", "app:")
        ):
            frame.module = generate_module(abs_path)
        if in_app is not None:
            frame.in_app = in_app

        # Source context — built into SourceMapCacheToken
        if token.context_line is not None:
            frame.context_line = token.context_line.rstrip("\n")
            frame.pre_context = [line.rstrip("\n") for line in token.pre_context]
            frame.post_context = [
                line.rstrip("\n") for line in token.post_context if line != ""
            ]

    def build_debug_id_map(self) -> dict[str, str]:
        debug_id_map = {}
        if self.data.debug_meta and self.data.debug_meta.images:
            for image in self.data.debug_meta.images:
                if image.type == "sourcemap" and image.code_file:
                    filename = image.code_file.split("/")[-1]
                    debug_id_map[filename] = str(image.debug_id)
        return debug_id_map

    def find_source_files(self, frame, debug_id_map):
        minified_filename = frame.abs_path.split("/")[-1] if frame.abs_path else ""
        debug_id = debug_id_map.get(minified_filename)
        # A debug id identifies the bundle exactly: it wins over any file name
        # match, wherever the bundles fall in the list.
        if debug_id:
            for debug_bundle in self.debug_bundles:
                if debug_bundle.debug_id and str(debug_id) == str(
                    debug_bundle.debug_id
                ):
                    return debug_bundle.file, debug_bundle.sourcemap_file

        # Fallback to matching by filename
        for debug_bundle in self.debug_bundles:
            if minified_filename == debug_bundle.file.name:
                return debug_bundle.file, debug_bundle.sourcemap_file
        return None

    async def remap_exception(self, exception: "EventException", debug_id_map):
        raw_stacktrace = None
        for frame in self.get_valid_frames(exception):
            source_files = self.find_source_files(frame, debug_id_map)
            if source_files is None:
                continue

            minified_source, map_file = source_files
            if not map_file:
                continue

            token = await self.lookup_token(frame, map_file, minified_source)
            if token is None:
                continue

            # Copy original stacktrace before modifying them
            if raw_stacktrace is None and exception.stacktrace:
                raw_stacktrace = copy.deepcopy(exception.stacktrace)

            self.process_frame(frame, token)

        if raw_stacktrace is not None:
            exception.raw_stacktrace = raw_stacktrace

    async def transform(self):
        exceptions = self.get_stacktrace_exceptions()
        if not exceptions or not self.debug_bundles:
            return

        # Map minified filenames to debug_ids from debug_meta
        debug_id_map = self.build_debug_id_map()
        for exception in exceptions:
            await self.remap_exception(exception, debug_id_map)
