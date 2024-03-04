import uuid
from typing import Dict, List, Tuple, Union
from urllib.parse import urlparse

from anonymizeip import anonymize_ip
from django.db import transaction
from django.db.models.expressions import OuterRef, RawSQL
from django.db.utils import IntegrityError
from ipware import get_client_ip
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.environments.models import Environment
from glitchtip.serializers import FlexibleDateTimeField
from issues.models import EventType, Issue
from issues.serializers import BaseBreadcrumbsSerializer
from issues.tasks import update_search_index_issue
from observability.metrics import events_counter, issues_counter
from apps.releases.models import Release
from sentry.eventtypes.base import DefaultEvent
from sentry.eventtypes.error import ErrorEvent

from .event_context_processors import EVENT_CONTEXT_PROCESSORS
from .event_processors import EVENT_PROCESSORS
from .event_tag_processors import TAG_PROCESSORS
from .fields import (
    ForgivingDisallowRegexField,
    ForgivingHStoreField,
    GenericField,
    QueryStringField,
)
from .models import Event, LogLevel
from .utils import generate_hash


def replace(data: Union[str, dict, list], match: str, repl: str):
    """A recursive replace function"""
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
        child=serializers.CharField(allow_blank=True, allow_null=True), required=False
    )
    # Dict values can be both str and List[str]
    headers = serializers.DictField(required=False)
    url = serializers.CharField(required=False, allow_blank=True)
    method = serializers.CharField(required=False, allow_blank=True)
    query_string = QueryStringField(required=False, allow_null=True)


class BreadcrumbsSerializer(BaseBreadcrumbsSerializer):
    timestamp = GenericField(required=False)

    def validate_level(self, value):
        if value == "log":
            return "info"
        return value


class BaseSerializer(serializers.Serializer):
    def process_user(self, project, data):
        """Fetch user data from SDK event and request"""
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
    """Represents events coming from a OSS sentry SDK client"""

    breadcrumbs = serializers.JSONField(required=False)
    fingerprint = serializers.ListField(child=serializers.CharField(), required=False)
    tags = ForgivingHStoreField(required=False)
    event_id = serializers.UUIDField(required=False, default=uuid.uuid4)
    extra = serializers.JSONField(required=False)
    request = RequestSerializer(required=False)
    server_name = serializers.CharField(required=False)
    sdk = serializers.JSONField(required=False)
    platform = serializers.CharField(required=False)
    release = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    environment = ForgivingDisallowRegexField(
        required=False, allow_null=True, disallow_regex=r"^[^\n\r\f\/]*$"
    )
    _meta = serializers.JSONField(required=False)

    def set_environment(self, name: str, project) -> str:
        if not project.environment_id and name:
            environment, _ = Environment.objects.get_or_create(
                name=name[: Environment._meta.get_field("name").max_length],
                organization=project.organization,
            )
            environment.projects.add(project)
            project.environment_id = environment.id
            return environment.name
        return name

    def set_release(self, version: str, project) -> str:
        """
        Set project.release_id if not already so
        Create needed Release if necessary
        """
        if not project.release_id and version:
            release, _ = Release.objects.get_or_create(
                version=version, organization=project.organization
            )
            release.projects.add(project)
            project.release_id = release.id
            return release.version
        return version


class FormattedMessageSerializer(serializers.Serializer):
    formatted = serializers.CharField(
        required=False
    )  # Documented as required, but some Sentry SDKs don't send it
    message = serializers.CharField(required=False)
    params = serializers.JSONField(required=False)

    def validate(self, attrs):
        data = super().validate(attrs)
        if not data.get("formatted") and data.get("params"):
            params = data["params"]
            if isinstance(params, list):
                data["formatted"] = data["message"] % tuple(params)
            elif isinstance(params, dict):
                data["formatted"] = data["message"].format(**params)
            return data
        # OSS Sentry only keeps unformatted "message" when it creates a formatted message
        return {key: data[key] for key in data if key != "message"}


class MessageField(serializers.CharField):
    def to_internal_value(self, data):
        if isinstance(data, dict):
            serializer = FormattedMessageSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            return serializer.validated_data
        return super().to_internal_value(data)


class LogEntrySerializer(serializers.Serializer):
    formatted = serializers.CharField(required=False)
    message = serializers.CharField(required=False, allow_blank=True)
    params = serializers.JSONField(required=False)

    def validate(self, attrs):
        data = super().validate(attrs)
        if not data.get("formatted") and data.get("params"):
            params = data["params"]
            if isinstance(params, list):
                data["formatted"] = data["message"] % tuple(data["params"])
            elif isinstance(params, dict):
                data["formatted"] = data["message"].format(**params)
        return data


class StoreDefaultSerializer(SentrySDKEventSerializer):
    """
    Default serializer. Used as both a base class and for default error types
    """

    type = EventType.DEFAULT
    contexts = serializers.JSONField(required=False)
    level = serializers.CharField(required=False)
    logentry = LogEntrySerializer(required=False)
    message = MessageField(required=False, allow_blank=True, allow_null=True)
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
        """Get event type class from self.type"""
        if self.type is EventType.DEFAULT:
            return DefaultEvent()
        if self.type is EventType.ERROR:
            return ErrorEvent()

    def modify_exception(self, exception):
        """OSS Sentry does this, I have no idea why"""
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

    def generate_tags(self, data: Dict, tags: List[Tuple[str, str]] = None):
        """
        Determine tag relational data

        Optionally pass tags array for existing known tags to generate
        """
        if tags is None:
            tags = []
        for Processor in TAG_PROCESSORS:
            processor = Processor()
            value = processor.get_tag_values(data)
            if value:
                tags.append((processor.tag, value))
        if data.get("tags"):
            tags += [(k, v) for k, v in data["tags"].items()]
        return tags

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
        """Prefer message over logentry"""
        if "message" in data:
            if isinstance(data["message"], dict):
                return data["message"].get("formatted") or data["message"].get(
                    "message", ""
                )
            return data["message"]
        return data.get("logentry", {}).get("message", "")

    def get_logentry(self, data):
        if "logentry" in data:
            return data.get("logentry")
        elif "message" in data:
            message = data["message"]
            if isinstance(message, dict):
                return message
            return {"formatted": message}

    def is_url(self, filename: str) -> bool:
        return filename.startswith(("file:", "http:", "https:", "applewebdata:"))

    def normalize_stacktrace(self, stacktrace):
        """
        Port of semaphore store/normalize/stacktrace.rs
        """
        if not stacktrace:
            return
        for frame in stacktrace.get("frames", []):
            if not frame.get("abs_path") and frame.get("filename"):
                frame["abs_path"] = frame["filename"]
            if frame.get("filename") and self.is_url(frame["filename"]):
                frame["filename"] = urlparse(frame["filename"]).path

    def create(self, validated_data):
        data = validated_data
        project = self.context.get("project")

        eventtype = self.get_eventtype()
        metadata = eventtype.get_metadata(data)
        exception = data.get("exception")
        if (
            data.get("stacktrace")
            and exception
            and len(exception.get("values", 0)) > 0
            and not exception["values"][0].get("stacktrace")
        ):
            # stacktrace is deprecated, but supported at this time
            # Assume it's for the first exception value
            exception["values"][0]["stacktrace"] = data.get("stacktrace")
        exception = self.modify_exception(exception)
        if isinstance(exception, dict):
            for value in exception.get("values", []):
                self.normalize_stacktrace(value.get("stacktrace"))

        tags = []
        release = self.set_release(data.get("release"), project)
        if project.release_id:
            tags.append(("release", release))
        environment = self.set_environment(data.get("environment"), project)
        if project.environment_id:
            tags.append(("environment", environment))
        if data.get("server_name"):
            tags.append(("server_name", data.get("server_name")))

        for Processor in EVENT_PROCESSORS:
            Processor(project, project.release_id, data).run()

        title = eventtype.get_title(metadata)
        culprit = eventtype.get_location(data)
        issue_hash = generate_hash(title, culprit, self.type, data.get("fingerprint"))
        request = data.get("request")
        breadcrumbs = data.get("breadcrumbs")
        level = None
        if data.get("level"):
            level = LogLevel.from_string(data["level"])
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
            defaults = {
                "metadata": sanitize_bad_postgres_json(metadata),
                "title": sanitize_bad_postgres_chars(title),
                "culprit": sanitize_bad_postgres_chars(culprit),
                "type": self.type,
            }
            if level:
                defaults["level"] = level

            tags = self.generate_tags(data, tags)
            defaults["tags"] = {tag[0]: [tag[1]] for tag in tags}

            issue_created = False
            # Similar to get_or_create but with multiple tables
            try:
                issue = Issue.objects.get(
                    project_id=project.id,
                    issuehash__value=issue_hash,
                )
            except Issue.DoesNotExist:
                with transaction.atomic():
                    issue = Issue.objects.create(project_id=project.id, **defaults)
                    try:
                        issue.issuehash_set.create(value=issue_hash, project=project)
                        issue_created = True
                    except IntegrityError:
                        issue = Issue.objects.get(
                            project_id=project.id,
                            issuehash__value=issue_hash,
                        )

            json_data = {
                "breadcrumbs": breadcrumbs,
                "contexts": contexts,
                "culprit": culprit,
                "exception": exception,
                "logentry": self.get_logentry(data),
                "metadata": metadata,
                "message": self.get_message(data),
                "modules": data.get("modules"),
                "platform": data.get("platform", "other"),
                "request": request,
                "sdk": data.get("sdk"),
                "title": title,
                "type": self.type.label,
            }

            if project.environment_id:
                json_data["environment"] = data.get("environment")
            if data.get("logentry"):
                json_data["logentry"] = data.get("logentry")

            extra = data.get("extra")
            if extra:
                json_data["extra"] = extra
            user = self.process_user(project, data)
            if user:
                json_data["user"] = user

            errors = None
            handled_errors = self.context.get("handled_errors")
            if handled_errors:
                errors = []
                for field_name, field_errors in handled_errors.items():
                    for error in field_errors:
                        errors.append(
                            {
                                "reason": str(error),
                                "type": error.code,
                                "name": field_name,
                                "value": error.value,
                            }
                        )

            params = {
                "event_id": data["event_id"],
                "issue": issue,
                "tags": {tag[0]: tag[1] for tag in tags},
                "errors": errors,
                "timestamp": data.get("timestamp"),
                "data": sanitize_bad_postgres_json(json_data),
                "release_id": project.release_id,
            }
            if level:
                params["level"] = level
            events_counter.labels(
                project.slug, project.organization.slug, issue.title
            ).inc()
            try:
                event = Event.objects.create(**params)
            except IntegrityError as err:
                # This except is more efficient than a query for exists().
                if err.args and "event_id" in err.args[0]:
                    raise PermissionDenied(
                        "An event with the same ID already exists (%s)"
                        % params["event_id"]
                    ) from err
                raise err

        if issue_created:  # Do it right now, so that new issues look correct
            issues_counter.labels(project.slug, project.organization.slug).inc()
            event_data = Event.objects.filter(issue_id=OuterRef("id")).values("data")[
                :1
            ]
            event_vector = event_data.annotate(
                search_vector=RawSQL("select generate_issue_tsvector(data)", [])
            ).values("search_vector")
            Issue.objects.filter(pk=issue.pk).update(
                search_vector=event_vector, last_seen=event.created
            )
        else:  # Updates can be slower and debounced
            issue.check_for_status_update()
            # Expire after 1 hour - in case of major backup
            update_search_index_issue(args=[issue.pk])

        return event


class StoreErrorSerializer(StoreDefaultSerializer):
    """Primary difference is the presense of exception attribute"""

    type = EventType.ERROR
    exception = serializers.JSONField(required=False)
    stacktrace = serializers.JSONField(
        required=False, help_text="Deprecated but supported at this time"
    )


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

    def create(self, validated_data):
        project = self.context.get("project")
        csp = validated_data["csp-report"]
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
        user = self.process_user(project, validated_data)
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
    event_id = serializers.UUIDField(required=False)
    sent_at = FlexibleDateTimeField(required=False)
