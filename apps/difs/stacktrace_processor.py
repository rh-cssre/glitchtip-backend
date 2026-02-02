import copy
import logging
import zipfile

import cxxfilt
from symbolic import Archive, ProguardMapper, SymCache, parse_addr

alternative_arch = {"x86": ["x86", "x86_64"]}


class ResolvedStacktrace:
    def __init__(self, score=0, frames=[]):
        self.score = score
        self.frames = frames


def find_arch_object(archive, arch):
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
    Find a source bundle (kind='src') matching the given debug_id.
    Returns the DebugInformationFile object or None.
    """
    try:
        from apps.difs.models import DebugInformationFile

        return DebugInformationFile.objects.filter(
            project_id=project_id, data__kind="src", data__debug_id=debug_id
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


class StacktraceProcessor:
    """
    This class process an event with exceptions. Try to load DIF and resolve
    the stacktrace
    """

    def __init__(self):
        pass

    @classmethod
    def is_supported(cls, event_json, dif):
        is_android = cls.is_android_event(event_json)
        is_proguard = dif.is_proguard_mapping()

        if is_android:
            return is_proguard

        return True

    @classmethod
    def resolve_stacktrace(cls, event, symbol_file, project_id=None, debug_id=None):
        # Process event
        try:
            contexts = event.get("contexts")
            if contexts is None:
                # Nodejs crash report doesn't contain this field.
                # In future, we need to support.
                return
            arch = contexts.get("device").get("arch")

            # Process the first exception only.
            exceptions = event.get("exception").get("values")
            stacktrace = exceptions[0].get("stacktrace")
        except Exception as e:
            getLogger().error(
                f"StacktraceProcessor: Invalid event: {event}, error: {e}"
            )
            return

        is_android = cls.is_android_event(event)

        if is_android:
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
            archive.open(symbol_file)
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
            for frame in frames:
                frame = copy.copy(frame)

                image_addr = parse_addr(frame.get("image_addr"))
                instruction_addr = parse_addr(frame.get("instruction_addr"))
                function = frame.get("function")
                addr = instruction_addr - image_addr
                symbol = sym_cache.lookup(addr)
                digested_symbol = digest_symbol(symbol)

                if digested_symbol is not None and digested_symbol.symbol == function:
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
            exceptions = data.get("exception").get("values")
            stacktrace = exceptions[0].get("stacktrace")
            stacktrace["frames"] = frames
            event.data = data
        except Exception as e:
            getLogger().error(f"StacktraceProcessor: Unexpected error: {e}")
            pass

    @classmethod
    def is_android_event(cls, event):
        try:
            return event["contexts"]["os"]["name"] == "Android"
        except Exception:
            return False
