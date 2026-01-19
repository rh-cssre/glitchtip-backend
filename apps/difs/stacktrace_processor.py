import copy
import logging

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
        item = symbol[0]
        if item.lang == "unknown":
            return None
        return item
    except Exception:
        pass


def getLogger():
    return logging.getLogger("glitchtip.difs")


class StacktraceProcessor:
    """
    This class process an event with exceptions. Try to load DIF and resolve
    the stacktrace
    """

    def __init__(self):
        pass

    @classmethod
    def is_supported(cls, event_json, dif):
        if cls.is_android_event(event_json):
            return dif.is_proguard_mapping()

        # It need more data to determine the exact condition
        return True

    @classmethod
    def resolve_stacktrace(cls, event, symbol_file):
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
        except Exception:
            getLogger().error(f"StacktraceProcessor: Invalid event: {event}")
            return

        if cls.is_android_event(event):
            return cls.resolve_proguard_stacktrace(stacktrace, symbol_file)

        return cls.resolve_native_stacktrace(stacktrace, symbol_file, arch=arch)

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
    def resolve_native_stacktrace(cls, stacktrace, symbol_file, arch=None):
        # Open symbol file
        try:
            archive = Archive.open(symbol_file)
            archive.open(symbol_file)
            obj = find_arch_object(archive, arch)
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
                    frame["filename"] = digested_symbol.filename
                    frame["lineNo"] = digested_symbol.line
                    try:
                        frame["function"] = cxxfilt.demangle(digested_symbol.symbol)
                    except cxxfilt.InvalidName:
                        frame["function"] = (
                            digested_symbol.symbol
                        )  # Keep original if demangling fails
                    score = score + 1

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
