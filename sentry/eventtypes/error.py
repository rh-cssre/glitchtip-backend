from django.utils.encoding import force_str

from sentry.culprit import generate_culprit
from sentry.stacktraces.functions import get_function_name_for_frame
from sentry.stacktraces.processing import get_crash_frame_from_event_data
from sentry.utils.safe import get_path, trim, truncatechars

from .base import BaseEvent


def get_crash_location(data):
    frame = get_crash_frame_from_event_data(
        data,
        frame_filter=lambda x: x.get("function")
        not in (None, "<redacted>", "<unknown>"),
    )
    if frame is not None:
        func = get_function_name_for_frame(frame, data.get("platform"))
        return frame.get("filename") or frame.get("abs_path"), func


class ErrorEvent(BaseEvent):
    key = "error"

    def get_metadata(self, data):
        # Check for undocumented interface where exception has no values. Go SDK does this.
        # https://docs.sentry.io/development/sdk-dev/event-payloads/exception/
        # exception can be an list instead of a dictionary
        if isinstance(data.get("exception"), list):
            if len(data["exception"]) == 0:
                return {}
            # Force documented interface
            data["exception"] = {"values": data["exception"]}
        exception = get_path(data, "exception", "values", -1)
        if not exception:
            return {}

        loc = get_crash_location(data)
        rv = {"value": trim(get_path(exception, "value", default=""), 1024)}

        # If the exception mechanism indicates a synthetic exception we do not
        # want to record the type and value into the metadata.
        if not get_path(exception, "mechanism", "synthetic"):
            rv["type"] = trim(get_path(exception, "type", default="Error"), 128)

        # Attach crash location if available
        if loc is not None:
            fn, func = loc
            if fn:
                rv["filename"] = fn
            if func:
                rv["function"] = func

        return rv

    def get_title(self, metadata):
        ty = metadata.get("type")
        if ty is None:
            return metadata.get("function") or "<unknown>"
        if not metadata.get("value"):
            return ty
        try:
            return "{}: {}".format(ty, truncatechars(metadata["value"].splitlines()[0]))
        except AttributeError:
            # GlitchTip modification
            # Exception value is specified as a string, sometimes it isn't. This is a fallback.
            return "{}: {}".format(ty, str(metadata["value"]))

    def get_location(self, data):
        return force_str(
            data.get("culprit")
            or data.get("transaction")
            or generate_culprit(data)
            or ""
        )
