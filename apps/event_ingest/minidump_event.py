import io
import logging
import struct
import uuid

from minidump.directory import MINIDUMP_STREAM_TYPE
from minidump.minidumpfile import MinidumpFile
from minidump.streams.ExceptionStream import ExceptionCode
from minidump.streams.ModuleListStream import MINIDUMP_MODULE_LIST
from minidump.streams.SystemInfoStream import PLATFORM_ID, PROCESSOR_ARCHITECTURE
from symbolic import normalize_debug_id

# Maps PROCESSOR_ARCHITECTURE enum to standard arch strings
ARCH_MAP = {
    PROCESSOR_ARCHITECTURE.AMD64: "x86_64",
    PROCESSOR_ARCHITECTURE.INTEL: "x86",
    PROCESSOR_ARCHITECTURE.ARM: "arm",
    PROCESSOR_ARCHITECTURE.AARCH64: "arm64",
    PROCESSOR_ARCHITECTURE.IA64: "ia64",
}

# Maps PLATFORM_ID to (os_name, debug_image_type)
PLATFORM_MAP = {
    PLATFORM_ID.VER_PLATFORM_WIN32s: ("Windows", "pe"),
    PLATFORM_ID.VER_PLATFORM_WIN32_WINDOWS: ("Windows", "pe"),
    PLATFORM_ID.VER_PLATFORM_WIN32_NT: ("Windows", "pe"),
    PLATFORM_ID.VER_PLATFORM_CRASHPAD_MAC: ("macOS", "macho"),
    PLATFORM_ID.VER_PLATFORM_CRASHPAD_IOS: ("iOS", "macho"),
    PLATFORM_ID.VER_PLATFORM_CRASHPAD_LINUX: ("Linux", "elf"),
    PLATFORM_ID.VER_PLATFORM_CRASHPAD_ANDROID: ("Android", "elf"),
    PLATFORM_ID.VER_PLATFORM_CRASHPAD_SOLARIS: ("Solaris", "elf"),
    PLATFORM_ID.VER_PLATFORM_CRASHPAD_FUSCHIA: ("Fuchsia", "elf"),
}

# Overrides for exception code display names where the enum member name
# doesn't produce the expected result after stripping the EXCEPTION_ prefix.
_EXCEPTION_NAME_OVERRIDES: dict[ExceptionCode, str] = {
    ExceptionCode.EXCEPTION_SIGIOT: "SIGABRT",  # SIGIOT is the obscure name for SIGABRT
}

# Instruction pointer offsets for each architecture's context structure.
# (offset_in_bytes, field_size_in_bytes)
_IP_OFFSETS: dict[PROCESSOR_ARCHITECTURE, tuple[int, int]] = {
    PROCESSOR_ARCHITECTURE.AMD64: (248, 8),  # Rip
    PROCESSOR_ARCHITECTURE.INTEL: (184, 4),  # Eip
    PROCESSOR_ARCHITECTURE.AARCH64: (256, 8),  # PC in Breakpad ARM64 context
    PROCESSOR_ARCHITECTURE.ARM: (60, 4),  # PC (r15) in Breakpad ARM context
}


def parse_cv_record_debug_id(cv_record_bytes: bytes) -> str | None:
    """Parse a CodeView PDB70 (RSDS) record to extract a normalized debug_id.

    The PDB70 record layout:
      - 4 bytes: signature (b'RSDS' = 0x53445352)
      - 16 bytes: GUID (mixed-endian UUID)
      - 4 bytes: age
      - variable: PDB filename (null-terminated UTF-8)
    """
    if len(cv_record_bytes) < 24 or cv_record_bytes[:4] != b"RSDS":
        return None

    guid_bytes = cv_record_bytes[4:20]
    (age,) = struct.unpack_from("<I", cv_record_bytes, 20)
    # GUID fields: Data1 (LE 4B), Data2 (LE 2B), Data3 (LE 2B), Data4 (BE 8B)
    a, b, c = struct.unpack_from("<IHH", guid_bytes, 0)
    d = guid_bytes[8:16]
    debug_id = f"{a:08x}-{b:04x}-{c:04x}-{d[:2].hex()}-{d[2:].hex()}"
    if age > 0:
        debug_id += f"-{age:x}"
    return normalize_debug_id(debug_id)


def _get_os_and_image_type(mf: MinidumpFile) -> tuple[str, str]:
    """Return (os_name, image_type) from system info, with defaults."""
    if mf.sysinfo and mf.sysinfo.PlatformId:
        os_name, image_type = PLATFORM_MAP.get(
            mf.sysinfo.PlatformId, ("Unknown", "elf")
        )
        return os_name, image_type
    return "Unknown", "elf"


def _get_arch(mf: MinidumpFile) -> str:
    """Return architecture string from system info."""
    if mf.sysinfo and mf.sysinfo.ProcessorArchitecture:
        return ARCH_MAP.get(mf.sysinfo.ProcessorArchitecture, "unknown")
    return "unknown"


def _read_cv_records(mf: MinidumpFile) -> list[bytes | None]:
    """Read raw CvRecord bytes for each module from the minidump.

    The high-level MinidumpModule doesn't preserve CvRecord data, so we
    re-parse the module list stream to access the raw MINIDUMP_MODULE
    structures which contain CvRecord location descriptors.
    """
    cv_records: list[bytes | None] = []
    for dir_entry in mf.directories:
        if dir_entry.StreamType == MINIDUMP_STREAM_TYPE.ModuleListStream:
            mf.file_handle.seek(dir_entry.Location.Rva)
            chunk = io.BytesIO(mf.file_handle.read(dir_entry.Location.DataSize))
            module_list = MINIDUMP_MODULE_LIST.parse(chunk)
            for mod in module_list.Modules:
                cv_data = None
                if mod.CvRecord.DataSize > 0 and mod.CvRecord.Rva > 0:
                    mf.file_handle.seek(mod.CvRecord.Rva)
                    cv_data = mf.file_handle.read(mod.CvRecord.DataSize)
                cv_records.append(cv_data)
            break
    return cv_records


def _extract_ip_from_context(
    context_rva: int,
    context_size: int,
    file_handle: io.BytesIO,
    arch: PROCESSOR_ARCHITECTURE | None,
) -> int | None:
    """Extract instruction pointer from a raw thread context."""
    if arch is None or arch not in _IP_OFFSETS:
        return None
    offset, size = _IP_OFFSETS[arch]
    if context_size < offset + size:
        return None
    file_handle.seek(context_rva + offset)
    raw = file_handle.read(size)
    if len(raw) < size:
        return None
    fmt = "<Q" if size == 8 else "<I"
    return struct.unpack(fmt, raw)[0]


def _find_module_for_addr(modules, addr: int) -> tuple[str | None, int | None]:
    """Find which module contains the given address.

    Returns (code_file, base_address) or (None, None).
    """
    if not modules:
        return None, None
    for mod in modules.modules:
        if mod.baseaddress <= addr < mod.endaddress:
            return mod.name, mod.baseaddress
    return None, None


def _build_debug_images(mf: MinidumpFile, image_type: str) -> list[dict]:
    """Build debug_meta.images list from minidump modules."""
    if not mf.modules:
        return []

    cv_records = _read_cv_records(mf)
    images = []

    for i, mod in enumerate(mf.modules.modules):
        image: dict = {
            "type": image_type,
            "image_addr": hex(mod.baseaddress),
            "image_size": mod.size,
            "code_file": mod.name,
        }
        if i < len(cv_records) and cv_records[i]:
            debug_id = parse_cv_record_debug_id(cv_records[i])
            if debug_id:
                image["debug_id"] = debug_id
        images.append(image)

    return images


def _get_exception_name(exc_code: ExceptionCode) -> str:
    """Get a human-readable exception name.

    Derives display names from the ExceptionCode enum member name:
    - Unix signals: strip EXCEPTION_ prefix (EXCEPTION_SIGSEGV → SIGSEGV)
    - Windows exceptions: keep the full name (EXCEPTION_ACCESS_VIOLATION)
    - Explicit overrides for edge cases (SIGIOT → SIGABRT)
    """
    if exc_code in _EXCEPTION_NAME_OVERRIDES:
        return _EXCEPTION_NAME_OVERRIDES[exc_code]
    name = exc_code.name
    # Strip EXCEPTION_ prefix only for Unix signals (result starts with SIG)
    if name.startswith("EXCEPTION_SIG"):
        return name[len("EXCEPTION_") :]
    return name


def minidump_to_event(data: bytes, sentry_meta: dict | None = None) -> dict:
    """Parse a minidump binary and return an error event dict.

    Args:
        data: Raw minidump file bytes (must start with b'MDMP').
        sentry_meta: Optional metadata from the 'sentry' multipart field
                     (release, environment, tags, etc.).

    Returns:
        A dict suitable for validation with WebIngestIssueEvent.
    """
    if sentry_meta is None:
        sentry_meta = {}

    # The minidump library logs a noisy "PEB parsing error!" at ERROR level
    # via the root logger when memory segments are absent (normal for
    # crashpad/breakpad minidumps).  Suppress during parsing.
    # Note: not fully thread-safe, but parsing is fast (<10ms) and this
    # runs in sync_to_async's thread pool, limiting blast radius.
    _root = logging.getLogger()
    _prev_level = _root.level
    _root.setLevel(logging.CRITICAL)
    try:
        mf = MinidumpFile.parse_bytes(data)
    finally:
        _root.setLevel(_prev_level)
    os_name, image_type = _get_os_and_image_type(mf)
    arch = _get_arch(mf)
    proc_arch = mf.sysinfo.ProcessorArchitecture if mf.sysinfo else None

    # --- Exception info ---
    exception_values = []
    crashing_thread_id = None
    crash_ip = None

    if mf.exception and mf.exception.exception_records:
        exc_stream = mf.exception.exception_records[0]
        exc_record = exc_stream.ExceptionRecord
        crashing_thread_id = exc_stream.ThreadId

        # ExceptionAddress is the instruction pointer at crash time
        crash_ip = exc_record.ExceptionAddress

        exc_name = _get_exception_name(exc_record.ExceptionCode)
        exc_value = f"Crash with signal {exc_name} at address {hex(crash_ip)}"

        # Build crashing frame
        frames = []
        if crash_ip is not None:
            package, image_addr = _find_module_for_addr(mf.modules, crash_ip)
            frame: dict = {
                "instruction_addr": hex(crash_ip),
                "in_app": True,
            }
            if package:
                frame["package"] = package
            if image_addr is not None:
                frame["image_addr"] = hex(image_addr)
            frames.append(frame)

        exc_entry: dict = {
            "type": exc_name,
            "value": exc_value,
            "mechanism": {"type": "minidump", "handled": False},
        }
        if frames:
            exc_entry["stacktrace"] = {"frames": frames}

        exception_values.append(exc_entry)

    # --- Threads ---
    thread_values = []
    if mf.threads:
        for thread in mf.threads.threads:
            tid = thread.ThreadId
            is_crashed = tid == crashing_thread_id

            thread_entry: dict = {
                "id": str(tid),
                "crashed": is_crashed,
                "current": is_crashed,
            }

            # For non-crashing threads, try to extract top frame from context
            if not is_crashed and thread.ThreadContext:
                ip = _extract_ip_from_context(
                    thread.ThreadContext.Rva,
                    thread.ThreadContext.DataSize,
                    mf.file_handle,
                    proc_arch,
                )
                if ip and ip != 0:
                    package, image_addr = _find_module_for_addr(mf.modules, ip)
                    frame = {"instruction_addr": hex(ip)}
                    if package:
                        frame["package"] = package
                    if image_addr is not None:
                        frame["image_addr"] = hex(image_addr)
                    thread_entry["stacktrace"] = {"frames": [frame]}

            thread_values.append(thread_entry)

    # --- Debug images ---
    debug_images = _build_debug_images(mf, image_type)

    # --- Build event ---
    event: dict = {
        "event_id": uuid.uuid4().hex,
        "platform": "native",
        "level": "fatal",
        "contexts": {
            "os": {"name": os_name, "type": "os"},
            "device": {"arch": arch, "type": "device"},
        },
    }

    if exception_values:
        event["exception"] = {"values": exception_values}

    if thread_values:
        event["threads"] = {"values": thread_values}

    if debug_images:
        event["debug_meta"] = {"images": debug_images}

    # Merge sentry metadata
    if sentry_meta.get("release"):
        event["release"] = sentry_meta["release"]
    if sentry_meta.get("environment"):
        event["environment"] = sentry_meta["environment"]
    if sentry_meta.get("tags"):
        event["tags"] = sentry_meta["tags"]

    return event
