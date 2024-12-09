import logging

import orjson
from django.conf import settings
from django.http import HttpRequest
from ninja.parser import Parser
from sentry_sdk import capture_message, set_context

logger = logging.getLogger(__name__)


class EnvelopeParser(Parser):
    def parse_body(self, request: HttpRequest):
        if (
            request.resolver_match
            and request.resolver_match.url_name == "event_envelope"
        ):
            if request.META.get("CONTENT_TYPE", None) in [
                "application/x-sentry-envelope",
                "application/octet-stream",
                "text/plain;charset=UTF-8",
                "text/plain",
                None,
            ]:
                result = [orjson.loads(line) for line in request.readlines()]
                if settings.EVENT_STORE_DEBUG:
                    print(orjson.dumps(result))
                return result

            try:
                return orjson.loads(request.body)
            except orjson.JSONDecodeError:
                set_context(
                    "incoming event",
                    {"body": request.body, "headers": request.META},
                )
                message = f"Envelope API unexpected content type {request.META.get('CONTENT_TYPE')}"
                capture_message(message, level="warning")
                logger.warning(message)

        return orjson.loads(request.body)
