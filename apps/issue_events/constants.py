from glitchtip.model_utils import FromStringIntegerChoices


class IssueEventType(FromStringIntegerChoices):
    DEFAULT = 0, "default"
    ERROR = 1, "error"
    CSP = 2, "csp"


class EventStatus(FromStringIntegerChoices):
    UNRESOLVED = 0, "unresolved"
    RESOLVED = 1, "resolved"
    IGNORED = 2, "ignored"


class LogLevel(FromStringIntegerChoices):
    NOTSET = 0, "sample"
    DEBUG = 1, "debug"
    INFO = 2, "info"
    WARNING = 3, "warning"
    ERROR = 4, "error"
    FATAL = 5, "fatal"


MAX_TAG_LENGTH = 255
