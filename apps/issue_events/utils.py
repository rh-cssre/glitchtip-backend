import string

from sentry.interfaces.stacktrace import get_context

from .constants import IssueEventType

digs = string.digits + string.ascii_uppercase


def base32_decode(base32_value: str) -> int:
    """
    Convert base32 string to integer
    Example 'A' -> 10
    """
    return int(base32_value, 32)


def int2base(x: int, base: int) -> str:
    """
    Convert base 10 integer to any base string that can be represented with numbers and
    upper case letters
    Example int2base(10, 32) -> 'A'
    Source: https://stackoverflow.com/a/2267446/443457
    """
    if x < 0:
        sign = -1
    elif x == 0:
        return digs[0]
    else:
        sign = 1
    x *= sign
    digits = []
    while x:
        digits.append(digs[int(x % base)])
        x = int(x / base)
    if sign < 0:
        digits.append("-")
    digits.reverse()
    return "".join(digits)


def base32_encode(base10_value: int) -> str:
    """
    Convert base 10 integer to base32 string
    Example 10 -> 'A'
    """
    return int2base(base10_value, 32)


def to_camel_with_lower_id(string: str) -> str:
    """For Sentry compatibility"""
    return "".join(
        word if i == 0 else "Id" if word == "id" else word.capitalize()
        for i, word in enumerate(string.split("_"))
    )


def get_entries(data):
    entries = []
    if exception := data.get("exception"):
        if isinstance(exception, list):  # Old format, delete after 2025
            exception = {"values": exception, "hasSystemFrames": False}
        elif isinstance(exception, dict):
            exception["hasSystemFrames"] = False
        # https://gitlab.com/glitchtip/sentry-open-source/sentry/-/blob/master/src/sentry/interfaces/stacktrace.py#L487
        # if any frame is "in_app" set this to True
        for value in exception["values"]:
            if (
                value.get("stacktrace", None) is not None
                and "frames" in value["stacktrace"]
            ):
                for frame in value["stacktrace"]["frames"]:
                    if frame.get("in_app") is True:
                        exception["hasSystemFrames"] = True
                    if "in_app" in frame:
                        frame["inApp"] = frame.pop("in_app")
                    if "abs_path" in frame:
                        frame["absPath"] = frame.pop("abs_path")
                    if "colno" in frame:
                        frame["colNo"] = frame.pop("colno")
                    if "lineno" in frame:
                        frame["lineNo"] = frame.pop("lineno")
                        pre_context = frame.pop("pre_context", None)
                        post_context = frame.pop("post_context", None)
                        if "context" not in frame:
                            frame["context"] = get_context(
                                frame["lineNo"],
                                frame.get("context_line"),
                                pre_context,
                                post_context,
                            )

        entries.append({"type": "exception", "data": exception})

    if breadcrumbs := data.get("breadcrumbs"):
        if isinstance(breadcrumbs, list):  # Old format, delete after 2025
            breadcrumbs = {"values": breadcrumbs}
        entries.append({"type": "breadcrumbs", "data": breadcrumbs})

    if logentry := data.get("logentry"):
        entries.append({"type": "message", "data": logentry})
    elif message := data.get("message"):
        entries.append({"type": "message", "data": {"formatted": message}})

    if request := data.get("request"):
        entries.append({"type": "request", "data": request})

    if csp := data.get("csp"):
        entries.append({"type": IssueEventType.CSP.label, "data": csp})
    return entries
