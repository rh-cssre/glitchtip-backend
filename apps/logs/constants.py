from glitchtip.model_utils import FromStringIntegerChoices


class LogLevel(FromStringIntegerChoices):
    """
    Log severity levels following OpenTelemetry semantic conventions.
    Maps to severity_number ranges in OTel spec.
    """

    TRACE = 0, "trace"
    DEBUG = 1, "debug"
    INFO = 2, "info"
    WARN = 3, "warn"
    ERROR = 4, "error"
    FATAL = 5, "fatal"


# Map string level names to LogLevel enum values
LEVEL_MAP = {
    "trace": LogLevel.TRACE,
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "warn": LogLevel.WARN,
    "warning": LogLevel.WARN,
    "error": LogLevel.ERROR,
    "fatal": LogLevel.FATAL,
}


def parse_level_filters(level_strings: list[str] | None) -> list[int] | None:
    """Convert a list of level name strings to their integer values.

    Unrecognized level names are silently ignored.
    """
    if not level_strings:
        return None
    values = []
    for level_str in level_strings:
        level_enum = LEVEL_MAP.get(level_str.lower())
        if level_enum is not None:
            values.append(level_enum)
    return values or None
