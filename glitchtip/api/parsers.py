from typing import cast

import orjson
from django.conf import settings
from django.http import HttpRequest
from ninja.parser import Parser
from ninja.types import DictStrAny


class EnvelopeParser(Parser):
    def parse_body(self, request: HttpRequest):
        if request.META.get("CONTENT_TYPE") == "application/x-sentry-envelope":
            result = [orjson.loads(line) for line in request.readlines()]
            if settings.EVENT_STORE_DEBUG:
                print(orjson.dumps(result))
            return result
        else:
            return orjson.loads(request.body)
