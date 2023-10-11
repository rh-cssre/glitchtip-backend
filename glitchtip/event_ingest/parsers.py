import json
from typing import cast

from django.http import HttpRequest
from ninja.types import DictStrAny
from ninja.parser import Parser


class EnvelopeParser(Parser):
    def parse_body(self, request: HttpRequest):
        if request.META.get("CONTENT_TYPE") == "application/x-sentry-envelope":
            foo = cast(list[DictStrAny], json.loads(request.body.decode()))
            print(len(foo))
            return foo
        else:
            return super().parse_body(request)

    def parse_envelope(self, body: bytes):
        return json.loads(body.decode())
