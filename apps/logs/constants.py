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
