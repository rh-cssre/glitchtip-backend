import logging
import re
import string

from sentry.interfaces.stacktrace import get_context

from .constants import IssueEventType

logger = logging.getLogger(__name__)

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


# Display form is "<PROJECT-SLUG>-<base32 short_id>"; slugs may contain hyphens,
# so the code is whatever follows the last one. ASCII-only: Unicode case folding
# would let characters like the Kelvin sign through to int(..., 32).
_SHORT_ID_RE = re.compile(
    r"^([a-z0-9_-]{1,50})-([0-9a-v]{1,7})$", re.IGNORECASE | re.ASCII
)
_MAX_SHORT_ID = 2147483647  # PositiveIntegerField


def parse_short_id(value: str) -> tuple[str, int] | None:
    """
    Parse a display short ID into (project slug, short_id)
    Example 'MY-PROJECT-1A' -> ('my-project', 42)
    """
    match = _SHORT_ID_RE.match(value.strip())
    if match is None:
        return None
    slug, code = match.groups()
    short_id = base32_decode(code)
    if short_id > _MAX_SHORT_ID:
        return None
    return slug.lower(), short_id


def to_camel_with_lower_id(string: str) -> str:
    """For Sentry compatibility"""
    return "".join(
        word if i == 0 else "Id" if word == "id" else word.capitalize()
        for i, word in enumerate(string.split("_"))
    )


def get_entries(data):
    entries = []
    # ``exception`` and ``breadcrumbs`` are stored in the shape ingest
    # normalizes to, ``{"values": [...]}``. Events ingested before that
    # normalization existed (5.1 and earlier) stored a bare list instead;
    # such a row can only exist on an install that kept it through the whole
    # 6.x series, so it is skipped rather than adapted. The check is for the
    # whole shape, values included: an entry without them renders as undefined
    # in the frontend, which is worse than the section being absent.
    exception = data.get("exception")
    if exception and not (
        isinstance(exception, dict) and isinstance(exception.get("values"), list)
    ):
        # Only reachable for a row written before 5.2.0 normalized the shape.
        # Log the shape, never the content: it is user-submitted event data.
        logger.warning(
            "Skipping event exception stored in an unsupported shape: %s",
            type(exception).__name__,
        )
    elif isinstance(exception, dict) and isinstance(exception.get("values"), list):
        exception["hasSystemFrames"] = False
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

    breadcrumbs = data.get("breadcrumbs")
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
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
