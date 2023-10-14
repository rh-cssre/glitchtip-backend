import orjson
from typing import cast

from django.http import HttpRequest
from ninja.types import DictStrAny
from ninja.parser import Parser


class EnvelopeParser(Parser):
    def parse_body(self, request: HttpRequest):
        if request.META.get("CONTENT_TYPE") == "application/x-sentry-envelope":
            foo = cast(list[DictStrAny], orjson.loads(request.body.decode()))
            return foo
        else:
            return orjson.loads(request.body)
