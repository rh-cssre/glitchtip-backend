import orjson
from django.conf import settings
from django.http import HttpRequest
from ninja.parser import Parser


class EnvelopeParser(Parser):
    def parse_body(self, request: HttpRequest):
        if request.META.get("CONTENT_TYPE") in [
            "application/x-sentry-envelope",
            "text/plain;charset=UTF-8",
        ]:
            result = [orjson.loads(line) for line in request.readlines()]
            if settings.EVENT_STORE_DEBUG:
                print(orjson.dumps(result))
            return result
        else:
            return orjson.loads(request.body)