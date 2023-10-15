from typing import cast

import orjson
from django.http import HttpRequest
from ninja.parser import Parser
from ninja.types import DictStrAny


class EnvelopeParser(Parser):
    def parse_body(self, request: HttpRequest):
        if request.META.get("CONTENT_TYPE") == "application/x-sentry-envelope":
            return [orjson.loads(line) for line in request.readlines()]
        else:
            return orjson.loads(request.body)
