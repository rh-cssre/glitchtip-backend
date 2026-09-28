import copy
import logging

from gt_rust.symbolic import demangle, normalize_debug_id, parse_addr

from apps.difs.models import DebugInformationFile
from apps.symbolication.sources import read_bundle_members, run_storage_read

alternative_arch = {"x86": ["x86", "x86_64"]}

# SymCache lookups take a uint64 address; anything outside this range cannot be
# probed at all and raises rather than returning no match.
UINT64_MAX = 0xFFFFFFFFFFFFFFFF

# Scoring one candidate base costs a lookup per address, and the event payload
# decides how many addresses there are, so score against a bounded sample.
MAX_SCORED_ADDRS = 32

# Debug image types that carry a native image_addr (mirrors NativeDebugImage).
NATIVE_IMAGE_TYPES = frozenset({"macho", "elf", "pe", "pe_dotnet", "wasm"})


def _parse_addr_or_none(value) -> int | None:
    """parse_addr, treating an unparseable event-supplied address as absent.

    parse_addr rejects magnitudes beyond i128 that unbounded int() parsing
    accepted; such values could never pass the uint64 range check anyway, so
    they should cost the event that one frame, not the whole stacktrace.
    """
    try:
        return parse_addr(value)
    except (ValueError, OverflowError):
        return None


def _normalize_debug_id_or_none(value) -> str | None:
    """normalize_debug_id, returning None for an id it can't parse.

    The DIF's own debug_id is not schema-validated, so it may not normalize.
    """
    if not value:
        return None
    try:
        return normalize_debug_id(str(value))
    except Exception:
        return None


class ResolvedStacktrace:
    def __init__(self, score=0, frames=None, context_requests=None):
        self.score = score
        self.frames = frames if frames is not None else []
        # (frame index, source path, line) for frames that want source
        # context from a source bundle; see add_native_source_context.
        self.context_requests = context_requests or []


def digest_symbol(symbol):
    try:
        if not symbol or len(symbol) == 0:
            return None
        item = symbol[0]
        # Removed lang == "unknown" check - symbols without source file info are still useful
        # They provide function names which help understand the call stack
        return item
    except Exception as e:
        getLogger().info(f"digest_symbol: exception {e}, symbol={symbol}")


def getLogger():
    return logging.getLogger("glitchtip.difs")


async def find_source_bundle(project_id, debug_id):
    """
    Find a source bundle matching the given debug_id.
    Matches kind='src' (test data) or kind='sources' (real uploads via symbolic.Archive).
    Returns the DebugInformationFile object (with its file blob) or None.
    """
    try:
        return (
            await DebugInformationFile.objects.filter(
                project_id=project_id,
                data__kind__in=["src", "sources"],
                data__debug_id=normalize_debug_id(debug_id),
            )
            .select_related("file__blob")
            .afirst()
        )
    except Exception as e:
        getLogger().error(f"find_source_bundle: Error finding source bundle: {e}")
        return None


async def read_source_files(source_bundle, file_paths) -> dict[str, list[str]]:
    """
    Source lines of ``file_paths`` from a source bundle, keyed by path; paths
    the bundle lacks are left out. Source bundles store files with a
    'files/' prefix. The zip is read in place, on the bounded storage read
    pool since storage has no async API.
    """
    members = {f"files{path}": path for path in file_paths}
    try:
        lines = await run_storage_read(
            read_bundle_members, source_bundle.file.blob, list(members)
        )
    except Exception as e:
        getLogger().error(f"read_source_files: Error extracting source: {e}")
        return {}
    return {members[member]: text for member, text in lines.items()}


def jvm_module_to_path(module, filename):
    """
    Convert JVM frame module + filename to a source file path for bundle lookup.

    sentry-cli bundle-jvm stores files with:
    - .jvm extension (replacing .java/.kt)
    - _/_/ prefix (corresponding to ~ in Sentry's source mapping)

    e.g. module="com.example.MyClass", filename="MyClass.java"
         -> "/_/_/com/example/MyClass.jvm"
    """
    if not module or not filename:
        return None
    # Change extension to .jvm (sentry-cli renames .java/.kt to .jvm)
    base, _, ext = filename.rpartition(".")
    if base and ext in ("java", "kt", "scala", "groovy"):
        jvm_filename = f"{base}.jvm"
    else:
        jvm_filename = filename
    # Extract package from module (everything before last dot)
    parts = module.rsplit(".", 1)
    if len(parts) == 2:
        package_path = parts[0].replace(".", "/")
        return f"/_/_/{package_path}/{jvm_filename}"
    # No package (default package)
    return f"/_/_/{jvm_filename}"


def _estimate_image_base(sym_cache, instruction_addrs):
    """Estimate the image base address when debug_meta is absent.

    Some SDKs (e.g. Flutter on Android) send native frames with absolute
    instruction_addr but no image_addr / debug_meta.  We recover the base by
    probing: pick one address, slide it across the sym_cache's valid range,
    and choose the base offset that resolves the most frames.
    """
    if not instruction_addrs:
        return 0

    probe = instruction_addrs[0]
    sample = instruction_addrs[:MAX_SCORED_ADDRS]
    best_base = 0
    best_score = 0
    threshold = max(len(sample) // 2, 1)

    for elf_offset in range(0, 0x500000, 0x1000):
        # SymCache.lookup returns a (possibly empty) list of SourceLocation.
        if not sym_cache.lookup(elf_offset):
            continue
        candidate_base = probe - elf_offset
        if candidate_base < 0:
            # elf_offset only grows, so no later candidate can be valid either.
            break
        score = sum(
            1
            for addr in sample
            # An address outside the base's addressable range is not part of
            # this image and cannot be probed.
            if candidate_base <= addr <= candidate_base + UINT64_MAX
            and sym_cache.lookup(addr - candidate_base)
        )
        if score > best_score:
            best_score = score
            best_base = candidate_base
            if score >= threshold:
                break
    return best_base


class StacktraceProcessor:
    """
    This class process an event with exceptions. Try to load DIF and resolve
    the stacktrace
    """

    def __init__(self):
        pass

    @classmethod
    def resolve_stacktrace(cls, event, cache, debug_id=None):
        """Resolve the first exception's frames with one loaded cache: a
        ProguardCache for an Android event without native frames, otherwise
        a SymCache."""
        # Process event
        try:
            contexts = event.get("contexts")
            if contexts is None:
                # Nodejs crash report doesn't contain this field.
                # In future, we need to support.
                return
            arch = (contexts.get("device") or {}).get("arch")

            # Process the first exception only.
            exceptions = (event.get("exception") or {}).get("values")
            stacktrace = exceptions[0].get("stacktrace")
            if stacktrace is None:
                return
        except Exception as e:
            getLogger().error(
                f"StacktraceProcessor: Invalid event: {event}, error: {e}"
            )
            return

        is_android = cls.is_android_event(event)
        native_frames = cls.has_native_frames(event)

        if is_android and not native_frames:
            return cls.resolve_proguard_stacktrace(stacktrace, cache)

        # Use the SDK's image_addr from debug_meta if present; it's more
        # reliable than guessing the base.
        image_addr_from_meta = None
        norm_debug_id = _normalize_debug_id_or_none(debug_id)
        if norm_debug_id:
            images = (event.get("debug_meta") or {}).get("images") or []
            for image in images:
                if image.get("type") not in NATIVE_IMAGE_TYPES:
                    continue
                img_id = image.get("debug_id")
                if img_id and normalize_debug_id(str(img_id)) == norm_debug_id:
                    image_addr_from_meta = image.get("image_addr")
                    break

        return cls.resolve_native_stacktrace(
            stacktrace,
            cache,
            arch=arch,
            debug_id=debug_id,
            image_addr_from_meta=image_addr_from_meta,
        )

    @classmethod
    def resolve_proguard_stacktrace(cls, stacktrace, mapper):
        try:
            frames = stacktrace.get("frames")
            score = 0
            resolved_frames = copy.copy(frames)
            for index, frame in enumerate(frames):
                if frame is None:
                    continue
                frame = copy.copy(frame)
                module = frame.get("module")
                function = frame.get("function")
                lineno = frame.get("lineno")
                if lineno is None:
                    continue
                result = mapper.remap_frame(module, function, lineno)
                if len(result) > 0:
                    remapped_frame = result[0]
                    frame["resolved"] = True
                    frame["filename"] = remapped_frame.file
                    frame["lineNo"] = remapped_frame.line
                    frame["function"] = remapped_frame.method
                    frame["module"] = remapped_frame.class_name
                    score = score + 1

                resolved_frames[index] = frame

            return ResolvedStacktrace(score=score, frames=resolved_frames)

        except Exception as e:
            getLogger().error(f"StacktraceProcessor: Unexpected error: {e}")

    @classmethod
    def resolve_native_stacktrace(
        cls,
        stacktrace,
        sym_cache,
        arch=None,
        debug_id=None,
        image_addr_from_meta=None,
    ):
        # A debug file for another architecture cannot resolve these frames.
        if arch is not None and sym_cache.arch not in alternative_arch.get(
            arch, [arch]
        ):
            return

        try:
            frames = stacktrace.get("frames")
            score = 0
            resolved_frames = []
            context_requests = []

            # A frame's own image_addr wins. For frames without one, fall back
            # to the base debug_meta supplies, and only then to estimation.
            estimated_base = 0
            meta_base = (
                _parse_addr_or_none(image_addr_from_meta)
                if image_addr_from_meta is not None
                else None
            )
            if meta_base is not None:
                # An SDK-supplied base beats guessing. An unparseable value
                # falls through so estimation still runs.
                estimated_base = meta_base
            elif frames and not any(f.get("image_addr") for f in frames if f):
                parsed = (
                    _parse_addr_or_none(f.get("instruction_addr"))
                    for f in frames
                    if f and f.get("instruction_addr")
                )
                addrs = [addr for addr in parsed if addr is not None]
                estimated_base = _estimate_image_base(sym_cache, addrs)

            for frame in frames:
                if frame is None:
                    resolved_frames.append(frame)
                    continue
                frame = copy.copy(frame)

                raw_image_addr = frame.get("image_addr")
                image_addr = (
                    _parse_addr_or_none(raw_image_addr)
                    if raw_image_addr is not None
                    else estimated_base
                )
                instruction_addr = _parse_addr_or_none(frame.get("instruction_addr"))
                if image_addr is None or instruction_addr is None:
                    # Unparseable addresses can't resolve; keep the frame as-is.
                    resolved_frames.append(frame)
                    continue
                addr = instruction_addr - image_addr
                if not 0 <= addr <= UINT64_MAX:
                    # The frame is outside this image - another object, or no
                    # instruction_addr at all, which parse_addr reports as 0.
                    # Probing it would abort the whole stacktrace.
                    resolved_frames.append(frame)
                    continue
                symbol = sym_cache.lookup(addr)
                digested_symbol = digest_symbol(symbol)

                if digested_symbol is not None:
                    frame["resolved"] = True
                    frame["filename"] = digested_symbol.full_path
                    frame["lineno"] = digested_symbol.line
                    # demangle returns the input unchanged when it can't
                    # demangle, so there is no failure case to handle.
                    frame["function"] = demangle(digested_symbol.symbol)
                    score = score + 1

                    # Source context comes from a source bundle, if one was
                    # uploaded; see add_native_source_context.
                    if (
                        debug_id
                        and digested_symbol.full_path
                        and digested_symbol.line > 0
                    ):
                        context_requests.append(
                            (
                                len(resolved_frames),
                                digested_symbol.full_path,
                                digested_symbol.line,
                            )
                        )

                resolved_frames.append(frame)

            return ResolvedStacktrace(
                score=score,
                frames=resolved_frames,
                context_requests=context_requests,
            )
        except Exception as e:
            getLogger().error(f"StacktraceProcessor: Unexpected error: {e}")

    @classmethod
    def update_frames(cls, event, frames):
        try:
            data = event.data
            exceptions = (data.get("exception") or {}).get("values")
            stacktrace = exceptions[0].get("stacktrace")
            stacktrace["frames"] = frames
            event.data = data
        except Exception as e:
            getLogger().error(f"StacktraceProcessor: Unexpected error: {e}")

    @classmethod
    def has_native_frames(cls, event):
        """Return True if any exception frame contains an instruction_addr.

        Native frames (ELF/Mach-O) carry instruction_addr; JVM/proguard frames
        carry module + function + lineno instead.  This lets us pick the right
        resolver without hard-coding SDK names.
        """
        try:
            for value in (event.get("exception") or {}).get("values", []):
                for frame in (value.get("stacktrace") or {}).get("frames", []):
                    if frame.get("instruction_addr"):
                        return True
        except Exception:
            pass
        return False

    @classmethod
    def is_android_event(cls, event):
        try:
            return event["contexts"]["os"]["name"] == "Android"
        except Exception:
            return False

    @classmethod
    async def add_native_source_context(cls, resolved, project_id, debug_id):
        """Fill source context into resolved native frames from the source
        bundle uploaded for ``debug_id``, if any."""
        if not resolved.context_requests:
            return
        source_bundle = await find_source_bundle(project_id, debug_id)
        if source_bundle is None:
            return
        paths = {path for _, path, _ in resolved.context_requests}
        sources = await read_source_files(source_bundle, paths)
        for index, path, line in resolved.context_requests:
            source_lines = sources.get(path)
            if source_lines and line <= len(source_lines):
                frame = resolved.frames[index]
                line_num = line - 1  # 0-indexed
                frame["context_line"] = source_lines[line_num]
                frame["pre_context"] = source_lines[max(0, line_num - 5) : line_num]
                frame["post_context"] = source_lines[
                    line_num + 1 : min(len(source_lines), line_num + 6)
                ]

    @classmethod
    async def resolve_jvm_source_context(cls, event_json, project_id, debug_ids):
        """
        Resolve source context for JVM stack frames using source bundles.
        Returns True if any frames were enriched.
        """
        try:
            exceptions = (event_json.get("exception") or {}).get("values")
            if not exceptions:
                return False
        except Exception as e:
            getLogger().error(f"resolve_jvm_source_context: Invalid event: {e}")
            return False

        # Find source bundles for all debug_ids
        bundles = []
        for debug_id in debug_ids:
            bundle = await find_source_bundle(project_id, debug_id)
            if bundle:
                bundles.append(bundle)

        if not bundles:
            return False

        def frames_needing_context():
            for exc in exceptions:
                stacktrace = exc.get("stacktrace")
                if not stacktrace:
                    continue
                for frame in stacktrace.get("frames") or []:
                    if frame.get("context_line"):
                        continue
                    lineno = frame.get("lineno")
                    if not lineno or lineno < 1:
                        continue
                    file_path = jvm_module_to_path(
                        frame.get("module"), frame.get("filename")
                    )
                    if file_path:
                        yield frame, file_path, lineno

        enriched = False
        for bundle in bundles:
            wanted = list(frames_needing_context())
            if not wanted:
                break
            sources = await read_source_files(bundle, {path for _, path, _ in wanted})
            for frame, file_path, lineno in wanted:
                source_lines = sources.get(file_path)
                if source_lines and lineno <= len(source_lines):
                    line_idx = lineno - 1
                    frame["context_line"] = source_lines[line_idx]
                    frame["pre_context"] = source_lines[max(0, line_idx - 5) : line_idx]
                    frame["post_context"] = source_lines[
                        line_idx + 1 : min(len(source_lines), line_idx + 6)
                    ]
                    enriched = True

        return enriched
