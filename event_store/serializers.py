from urllib.parse import urlparse
from django.db import transaction
from rest_framework import serializers
from sentry.eventtypes.error import ErrorEvent
from sentry.eventtypes.base import DefaultEvent
from issues.models import EventType, Event, Issue


class StoreDefaultSerializer(serializers.Serializer):
    """
    Default serializer. Used as both a base class and for default error types
    """

    type = EventType.DEFAULT
    breadcrumbs = serializers.JSONField(required=False)
    contexts = serializers.JSONField(required=False)
    event_id = serializers.UUIDField()
    extra = serializers.JSONField(required=False)
    level = serializers.CharField()
    logentry = serializers.JSONField(required=False)
    message = serializers.CharField(required=False)
    platform = serializers.CharField()
    release = serializers.CharField(required=False)
    request = serializers.JSONField(required=False)
    sdk = serializers.JSONField()
    timestamp = serializers.DateTimeField(required=False)
    transaction = serializers.CharField(required=False)
    modules = serializers.JSONField(required=False)

    def get_eventtype(self):
        """ Get event type class from self.type """
        if self.type is EventType.DEFAULT:
            return DefaultEvent()
        if self.type is EventType.ERROR:
            return ErrorEvent()

    def modify_exception(self, exception):
        """ OSS Sentry does this, I have no idea why """
        if exception:
            for value in exception.get("values", []):
                value.pop("module", None)
                if value.get("stacktrace") and value["stacktrace"].get("frames"):
                    frames = value["stacktrace"]["frames"]
                    # If in_app is always true, make it false ¯\_(ツ)_/¯
                    if all(x.get("in_app") for x in frames):
                        for frame in frames:
                            frame["in_app"] = False
        return exception

    def create(self, project_id: int, data):
        eventtype = self.get_eventtype()
        metadata = eventtype.get_metadata(data)
        title = eventtype.get_title(metadata)
        culprit = eventtype.get_location(data)
        request = data.get("request")
        exception = self.modify_exception(data.get("exception"))
        if request:
            headers = request.get("headers")
            if headers:
                request["inferred_content_type"] = headers.get("Content-Type")
                request["headers"] = sorted([pair for pair in headers.items()])
        with transaction.atomic():
            issue, _ = Issue.objects.get_or_create(
                title=title,
                culprit=culprit,
                project_id=project_id,
                type=self.type,
                defaults={"metadata": metadata},
            )
            params = {
                "event_id": data["event_id"],
                "issue": issue,
                "timestamp": data.get("timestamp"),
                "data": {
                    "contexts": data.get("contexts"),
                    "culprit": culprit,
                    "exception": exception,
                    "metadata": metadata,
                    "modules": data.get("modules"),
                    "platform": data["platform"],
                    "request": request,
                    "sdk": data["sdk"],
                    "title": title,
                    "type": self.type.label,
                },
            }
            event = Event.objects.create(**params)
            issue.check_for_status_update()
        return event


class StoreErrorSerializer(StoreDefaultSerializer):
    """ Primary difference is the presense of exception attribute """

    type = EventType.ERROR
    exception = serializers.JSONField(required=False)


class StoreCSPReportSerializer(serializers.Serializer):
    """
    CSP Report Serializer
    Very different format from others Store serializers.
    Does not extend base class due to differences.
    """

    type = EventType.CSP

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # This is done to support the hyphen
        self.fields.update({"csp-report": serializers.JSONField()})

    def create(self, project_id: int, data):
        csp = data["csp-report"]
        title = self.get_title(csp)
        culprit = self.get_culprit(csp)
        uri = self.get_uri(csp)
        directive = self.get_effective_directive(csp)
        metadata = {
            "message": title,
            "uri": uri,
            "directive": directive,
        }
        issue, _ = Issue.objects.get_or_create(
            title=title,
            culprit=culprit,
            project_id=project_id,
            type=EventType.CSP,
            defaults={"metadata": metadata},
        )
        # Convert - to _
        normalized_csp = dict((k.replace("-", "_"), v) for k, v in csp.items())
        if "effective_directive" not in normalized_csp:
            normalized_csp["effective_directive"] = directive
        params = {
            "issue": issue,
            "data": {
                "culprit": culprit,
                "csp": normalized_csp,
                "title": title,
                "metadata": metadata,
                "message": title,
                "type": EventType.CSP.label,
            },
        }
        return Event.objects.create(**params)

    def get_effective_directive(self, data):
        """
        Some browers return effective-directive and others don't.
        Infer missing ones from violated directive
        """
        if "effective-directive" in data:
            return data["effective-directive"]
        first_violation = data["violated-directive"].split()[0]
        return first_violation

    def get_uri(self, data):
        url = data["blocked-uri"]
        return urlparse(url).netloc

    def get_title(self, data):
        effective_directive = self.get_effective_directive(data)
        humanized_directive = effective_directive.replace("-src", "")
        uri = self.get_uri(data)
        return f"Blocked '{humanized_directive}' from '{uri}'"

    def get_culprit(self, data):
        # "style-src cdn.example.com"
        return data.get("violated-directive")
