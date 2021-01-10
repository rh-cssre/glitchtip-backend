from typing import Dict, List, Tuple, Union
from urllib.parse import urlparse
from django.db import transaction, connection
from django.db.utils import IntegrityError
from ipware import get_client_ip
from anonymizeip import anonymize_ip
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from sentry.eventtypes.error import ErrorEvent
from sentry.eventtypes.base import DefaultEvent
from issues.models import EventType, Issue
from issues.serializers import BaseBreadcrumbsSerializer
from environments.models import Environment
from releases.models import Release
from glitchtip.serializers import FlexibleDateTimeField
from .models import Event, EventTagKey
from .event_tag_processors import TAG_PROCESSORS
from .event_context_processors import EVENT_CONTEXT_PROCESSORS


def replace(data: Union[str, dict, list], match: str, repl: str):
    """ A recursive replace function """
    if isinstance(data, dict):
        return {k: replace(v, match, repl) for k, v in data.items()}
    elif isinstance(data, list):
        return [replace(i, match, repl) for i in data]
    elif isinstance(data, str):
        return data.replace(match, repl)
    return data


def sanitize_bad_postgres_chars(data: Union[str, dict, list]):
    """
    Remove values which are not supported by the postgres string data types
    """
    known_bads = ["\x00"]
    for known_bad in known_bads:
        data = data.replace(known_bad, " ")
    return data


def sanitize_bad_postgres_json(data: Union[str, dict, list]):
    """
    Remove values which are not supported by the postgres JSONB data type
    """
    known_bads = ["\u0000"]
    for known_bad in known_bads:
        data = replace(data, known_bad, " ")
    return data


class RequestSerializer(serializers.Serializer):
    env = serializers.DictField(
        child=serializers.CharField(allow_blank=True), required=False
    )
    # Dict values can be both str and List[str]
    headers = serializers.DictField(required=False)
    url = serializers.CharField(required=False, allow_blank=True)
    method = serializers.CharField(required=False, allow_blank=True)
    query_string = serializers.CharField(required=False, allow_blank=True)


class GenericField(serializers.Field):
    def to_internal_value(self, data):
        return data


class BreadcrumbsSerializer(BaseBreadcrumbsSerializer):
    timestamp = GenericField(required=False)

    def validate_level(self, value):
        if value == "log":
            return "info"
        return value


class BaseSerializer(serializers.Serializer):
    def process_user(self, project, data):
        """ Fetch user data from SDK event and request """
        user = data.get("user", {})
        if self.context and self.context.get("request"):
            client_ip, is_routable = get_client_ip(self.context["request"])
            if user or is_routable:
                if is_routable:
                    if project.should_scrub_ip_addresses:
                        client_ip = anonymize_ip(client_ip)
                    user["ip_address"] = client_ip
                return user


class SentrySDKEventSerializer(BaseSerializer):
    """ Represents events coming from a OSS sentry SDK client """

    breadcrumbs = serializers.JSONField(required=False)
    tags = serializers.DictField(child=serializers.CharField(), required=False)
    event_id = serializers.UUIDField()
    extra = serializers.JSONField(required=False)
    request = RequestSerializer(required=False)
    server_name = serializers.CharField(required=False)
    sdk = serializers.JSONField()
    platform = serializers.CharField()
    release = serializers.CharField(required=False)
    environment = serializers.CharField(required=False)
    _meta = serializers.JSONField(required=False)


class StoreDefaultSerializer(SentrySDKEventSerializer):
    """
    Default serializer. Used as both a base class and for default error types
    """

    type = EventType.DEFAULT
    contexts = serializers.JSONField(required=False)
    level = serializers.CharField(required=False)
    logentry = serializers.JSONField(required=False)
    message = serializers.CharField(required=False)
    timestamp = FlexibleDateTimeField(required=False)
    transaction = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    user = serializers.JSONField(required=False)
    modules = serializers.JSONField(required=False)

    def validate_breadcrumbs(self, value):
        """
        Normalize breadcrumbs, which may come in as dict or list
        """
        if isinstance(value, list):
            value = {"values": value}
        if value.get("values") == []:
            return None
        serializer = BreadcrumbsSerializer(data=value.get("values"), many=True)
        if serializer.is_valid():
            return {"values": serializer.validated_data}
        return value

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

    def generate_tags(self, event: Event, data: Dict, tags: List[Tuple[str, str]] = []):
        """
        Determine tag relational data

        Optionally pass tags array for existing known tags to generate
        """
        for Processor in TAG_PROCESSORS:
            processor = Processor()
            value = processor.get_tag_values(data)
            if value:
                tags.append((processor.tag, value))
        if data.get("tags"):
            tags += [(k, v) for k, v in data["tags"].items()]
        self.save_tags(event, tags)

    def save_tags(self, event, tags: List[Tuple[str, str]]):
        """ Commit tags to database """
        tag_key_values = []
        # Get tag keys with 1 query (New ones are rarely created)
        event_tag_keys = EventTagKey.objects.filter(key__in=[tag[0] for tag in tags])
        for tag, value in tags:
            tag_key = next((x for x in event_tag_keys if x.key == tag), None)
            if tag_key is None:  # If there's a new tag key, create it
                tag_key, _ = EventTagKey.objects.get_or_create(key=tag)
            tag_key_values.append((tag_key.id, value))

        # add_event_tags adds event tag value (if necessary) and into event.tags
        if tag_key_values:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select add_event_tags(%s::uuid, %s::tag_key_value[]);",
                    [event.event_id, tag_key_values],
                )

    def annotate_contexts(self, event):
        """
        SDK events may contain contexts. This function adds additional contexts data
        """
        contexts = event.get("contexts")
        for Processor in EVENT_CONTEXT_PROCESSORS:
            processor = Processor()
            if contexts is None or not contexts.get(processor.name):
                processor_contexts = processor.get_context(event)
                if processor_contexts:
                    if contexts is None:
                        contexts = {}
                    contexts[processor.name] = processor_contexts
        return contexts

    def get_message(self, data):
        return data.get("logentry", {}).get("message", "")

    def get_environment(self, name: str, project):
        environment, _ = Environment.objects.get_or_create(
            name=name, organization=project.organization
        )
        environment.projects.add(project)
        return environment

    def get_release(self, version: str, project):
        release, _ = Release.objects.get_or_create(
            version=version, organization=project.organization
        )
        release.projects.add(project)
        return release

    def create(self, data):
        project = self.context.get("project")
        eventtype = self.get_eventtype()
        metadata = eventtype.get_metadata(data)
        title = eventtype.get_title(metadata)
        culprit = eventtype.get_location(data)
        request = data.get("request")
        breadcrumbs = data.get("breadcrumbs")
        exception = self.modify_exception(data.get("exception"))
        if request:
            headers = request.get("headers")
            if headers:
                request["inferred_content_type"] = headers.get("Content-Type")
                sorted_headers = sorted([pair for pair in headers.items()])
                for idx, header in enumerate(sorted_headers):
                    if isinstance(header[1], list):
                        sorted_headers[idx] = (header[0], header[1][0])
                request["headers"] = sorted_headers
        contexts = self.annotate_contexts(data)
        data["contexts"] = contexts

        with transaction.atomic():
            if not project.first_event:
                project.first_event = data.get("timestamp")
                project.save(update_fields=["first_event"])
            issue, _ = Issue.objects.get_or_create(
                title=sanitize_bad_postgres_chars(title),
                culprit=sanitize_bad_postgres_chars(culprit),
                project_id=project.id,
                type=self.type,
                defaults={"metadata": sanitize_bad_postgres_json(metadata)},
            )

            environment = None
            if data.get("environment"):
                environment = self.get_environment(data["environment"], project)
            release = None
            if data.get("release"):
                release = self.get_release(data["release"], project)

            json_data = {
                "breadcrumbs": breadcrumbs,
                "contexts": contexts,
                "culprit": culprit,
                "exception": exception,
                "metadata": metadata,
                "message": self.get_message(data),
                "modules": data.get("modules"),
                "platform": data["platform"],
                "request": request,
                "sdk": data["sdk"],
                "title": title,
                "type": self.type.label,
            }

            if environment:
                json_data["environment"] = environment.name

            extra = data.get("extra")
            if extra:
                json_data["extra"] = extra
            user = self.process_user(project, data)
            if user:
                json_data["user"] = user

            params = {
                "event_id": data["event_id"],
                "issue": issue,
                "timestamp": data.get("timestamp"),
                "data": sanitize_bad_postgres_json(json_data),
                "release": release,
            }
            try:
                event = Event.objects.create(**params)
            except IntegrityError as e:
                # This except is more efficient than a query for exists().
                if e.args and "event_id" in e.args[0]:
                    raise PermissionDenied(
                        "An event with the same ID already exists (%s)"
                        % params["event_id"]
                    ) from e
                raise e

        issue.check_for_status_update()

        tags = []
        if environment:
            tags.append(("environment", environment.name))
        if release:
            tags.append(("release", release.version))
        self.generate_tags(event, data, tags)

        return event


class StoreErrorSerializer(StoreDefaultSerializer):
    """ Primary difference is the presense of exception attribute """

    type = EventType.ERROR
    exception = serializers.JSONField(required=False)


class StoreCSPReportSerializer(BaseSerializer):
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

    def create(self, data):
        project = self.context.get("project")
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
            project_id=project.id,
            type=EventType.CSP,
            defaults={"metadata": metadata},
        )
        # Convert - to _
        normalized_csp = dict((k.replace("-", "_"), v) for k, v in csp.items())
        if "effective_directive" not in normalized_csp:
            normalized_csp["effective_directive"] = directive

        json_data = {
            "culprit": culprit,
            "csp": normalized_csp,
            "title": title,
            "metadata": metadata,
            "message": title,
            "type": EventType.CSP.label,
        }
        user = self.process_user(project, data)
        if user:
            json_data["user"] = user

        params = {
            "issue": issue,
            "data": json_data,
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


class EnvelopeHeaderSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    sent_at = FlexibleDateTimeField(required=False)
