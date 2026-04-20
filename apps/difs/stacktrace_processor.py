import contextlib
import copy
import logging
import zipfile

import cxxfilt
from symbolic import Archive, ProguardMapper, SymCache, normalize_debug_id, parse_addr

alternative_arch = {"x86": ["x86", "x86_64"]}


class ResolvedStacktrace:
    def __init__(self, score=0, frames=[]):
        self.score = score
        self.frames = frames


def find_arch_object(archive, arch):
    if arch is None:
        # No arch in event context — use the first object in the archive.
        # Each uploaded DIF typically contains a single architecture.
        for obj in archive.iter_objects():
            return obj
        return None

    if arch in alternative_arch:
        arch_list = alternative_arch[arch]
    else:
        arch_list = [arch]

    for arch in arch_list:
        try:
            object = archive.get_object(arch=arch)
            return object
        except Exception:
            pass


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
        pass


def getLogger():
    return logging.getLogger("glitchtip.difs")


def find_source_bundle(project_id, debug_id):
    """
    Find a source bundle matching the given debug_id.
    Matches kind='src' (test data) or kind='sources' (real uploads via symbolic.Archive).
    Returns the DebugInformationFile object or None.
    """
    try:
        from apps.difs.models import DebugInformationFile

        return DebugInformationFile.objects.filter(
            project_id=project_id,
            data__kind__in=["src", "sources"],
            data__debug_id=normalize_debug_id(debug_id),
        ).first()
    except Exception as e:
        getLogger().error(f"find_source_bundle: Error finding source bundle: {e}")
        return None


def extract_source_from_bundle(source_bundle, file_path):
    """
    Extract source code lines from a source bundle ZIP file.
    Returns a list of lines or None if extraction fails.
    """
    try:
        from apps.difs.tasks import difs_concat_file_blobs_to_disk

        with difs_concat_file_blobs_to_disk([source_bundle.file.blob]) as temp_file:
            with zipfile.ZipFile(temp_file.name, "r") as zf:
                # Source bundles store files with a 'files/' prefix
                bundle_path = f"files{file_path}"

                if bundle_path in zf.namelist():
                    content = zf.read(bundle_path).decode("utf-8")
                    return content.splitlines()
                else:
                    return None
    except Exception as e:
        getLogger().error(f"extract_source_from_bundle: Error extracting source: {e}")
        return None


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


@contextlib.contextmanager
def open_source_bundle(source_bundle_dif):
    """
    Context manager that opens a source bundle ZIP once and yields a lookup function.
    Avoids re-opening the ZIP for every frame.
    """
    from apps.difs.tasks import difs_concat_file_blobs_to_disk

    with difs_concat_file_blobs_to_disk([source_bundle_dif.file.blob]) as temp_file:
        with zipfile.ZipFile(temp_file.name, "r") as zf:
            namelist = set(zf.namelist())

            def get_source_lines(file_path):
                bundle_path = f"files{file_path}"
                if bundle_path in namelist:
                    return zf.read(bundle_path).decode("utf-8").splitlines()
                return None

            yield get_source_lines


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
    best_base = 0
    best_score = 0
    threshold = max(len(instruction_addrs) // 2, 1)

    for elf_offset in range(0, 0x500000, 0x1000):
        if next(sym_cache.lookup(elf_offset), None) is None:
            continue
        candidate_base = probe - elf_offset
        if candidate_base < 0:
            continue
        score = sum(
            1
            for addr in instruction_addrs
            if next(sym_cache.lookup(addr - candidate_base), None) is not None
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
    def resolve_stacktrace(cls, event, symbol_file, project_id=None, debug_id=None):
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
            return cls.resolve_proguard_stacktrace(stacktrace, symbol_file)

        return cls.resolve_native_stacktrace(
            stacktrace, symbol_file, arch=arch, project_id=project_id, debug_id=debug_id
        )

    @classmethod
    def resolve_proguard_stacktrace(cls, stacktrace, symbol_file):
        try:
            mapper = ProguardMapper.open(symbol_file)
        except Exception as e:
            getLogger().error(f"StacktraceProcessor: Open symbol file failed: {e}")
            return

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
        cls, stacktrace, symbol_file, arch=None, project_id=None, debug_id=None
    ):
        # Open symbol file
        try:
            archive = Archive.open(symbol_file)
            obj = find_arch_object(archive, arch)
            if obj is None:
                return
            sym_cache = SymCache.from_object(obj)
        except Exception as e:
            getLogger().error(f"StacktraceProcessor: Open symbol file failed: {e}")
            return

        try:
            frames = stacktrace.get("frames")
            score = 0
            resolved_frames = []

            # When frames lack image_addr (e.g. Flutter Android without
            # debug_meta), estimate the image base so relative offsets
            # land inside the symbol cache.
            estimated_base = 0
            if frames and not any(
                f.get("image_addr") for f in frames if f
            ):
                addrs = [
                    parse_addr(f.get("instruction_addr"))
                    for f in frames
                    if f and f.get("instruction_addr")
                ]
                estimated_base = _estimate_image_base(sym_cache, addrs)

            for frame in frames:
                if frame is None:
                    resolved_frames.append(frame)
                    continue
                frame = copy.copy(frame)

                raw_image_addr = frame.get("image_addr")
                image_addr = (
                    parse_addr(raw_image_addr)
                    if raw_image_addr is not None
                    else estimated_base
                )
                instruction_addr = parse_addr(frame.get("instruction_addr"))
                addr = instruction_addr - image_addr
                symbol = sym_cache.lookup(addr)
                digested_symbol = digest_symbol(symbol)

                if digested_symbol is not None:
                    frame["resolved"] = True
                    frame["filename"] = digested_symbol.full_path
                    frame["lineno"] = digested_symbol.line
                    try:
                        frame["function"] = cxxfilt.demangle(digested_symbol.symbol)
                    except cxxfilt.InvalidName:
                        frame["function"] = (
                            digested_symbol.symbol
                        )  # Keep original if demangling fails
                    score = score + 1

                    # Extract source context from source bundle if available
                    if (
                        project_id
                        and debug_id
                        and digested_symbol.full_path
                        and digested_symbol.line > 0
                    ):
                        source_bundle = find_source_bundle(project_id, debug_id)
                        if source_bundle:
                            source_lines = extract_source_from_bundle(
                                source_bundle, digested_symbol.full_path
                            )
                            if source_lines and digested_symbol.line <= len(
                                source_lines
                            ):
                                line_num = digested_symbol.line - 1  # 0-indexed
                                frame["context_line"] = source_lines[line_num]
                                frame["pre_context"] = source_lines[
                                    max(0, line_num - 5) : line_num
                                ]
                                frame["post_context"] = source_lines[
                                    line_num + 1 : min(len(source_lines), line_num + 6)
                                ]

                resolved_frames.append(frame)

            return ResolvedStacktrace(score=score, frames=resolved_frames)
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
            pass

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
    def resolve_jvm_source_context(cls, event_json, project_id, debug_ids):
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
            bundle = find_source_bundle(project_id, debug_id)
            if bundle:
                bundles.append(bundle)

        if not bundles:
            return False

        enriched = False
        for bundle in bundles:
            try:
                with open_source_bundle(bundle) as get_source_lines:
                    for exc in exceptions:
                        stacktrace = exc.get("stacktrace")
                        if not stacktrace:
                            continue
                        frames = stacktrace.get("frames")
                        if not frames:
                            continue

                        for frame in frames:
                            if frame.get("context_line"):
                                continue

                            module = frame.get("module")
                            filename = frame.get("filename")
                            lineno = frame.get("lineno")
                            if not lineno or lineno < 1:
                                continue

                            file_path = jvm_module_to_path(module, filename)
                            if not file_path:
                                continue

                            source_lines = get_source_lines(file_path)
                            if source_lines and lineno <= len(source_lines):
                                line_idx = lineno - 1
                                frame["context_line"] = source_lines[line_idx]
                                frame["pre_context"] = source_lines[
                                    max(0, line_idx - 5) : line_idx
                                ]
                                frame["post_context"] = source_lines[
                                    line_idx + 1 : min(len(source_lines), line_idx + 6)
                                ]
                                enriched = True
            except Exception as e:
                getLogger().error(
                    f"resolve_jvm_source_context: Error reading bundle: {e}"
                )

        return enriched
