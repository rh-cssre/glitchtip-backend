import logging
import re
import uuid
from datetime import datetime
from urllib.parse import parse_qs

from anonymizeip import anonymize_ip
from django.conf import settings
from django.utils.timezone import make_aware
from ipware import get_client_ip
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail, ValidationError

from apps.environments.models import Environment
from apps.releases.models import Release

from .models import Span, TransactionEvent, TransactionGroup

logger = logging.getLogger(__name__)


class FlexibleDateTimeField(serializers.DateTimeField):
    """Supports both DateTime and unix epoch timestamp"""

    def to_internal_value(self, value):
        try:
            return make_aware(datetime.fromtimestamp(float(value)))
        except (ValueError, TypeError):
            return super().to_internal_value(value)


class ErrorValueDetail(ErrorDetail):
    """Extended ErrorDetail with validation value"""

    value = None

    def __new__(cls, string, code=None, value=None):
        self = super().__new__(cls, string, code)
        self.value = value
        return self

    def __repr__(self):
        return "ErrorDetail(string=%r, code=%r, value=%r)" % (
            str(self),
            self.code,
            self.value,
        )


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


class ForgivingFieldMixin:
    def update_handled_errors_context(self, errors: list):
        if errors:
            handled_errors = self.context.get("handled_errors", {})
            self.context["handled_errors"] = handled_errors | {self.field_name: errors}


class ForgivingHStoreField(ForgivingFieldMixin, serializers.HStoreField):
    def run_child_validation(self, data):
        result = {}
        errors: list = []

        for key, value in data.items():
            if value is None:
                continue
            key = str(key)

            try:
                result[key] = self.child.run_validation(value)
            except ValidationError as e:
                for detail in e.detail:
                    errors.append(ErrorValueDetail(str(detail), detail.code, value))

        if errors:
            self.update_handled_errors_context(errors)
        return result


class QueryStringField(serializers.ListField):
    """
    Can be given as unparsed string, dictionary, or list of tuples
    Should store as List[List[str]] where inner List is always of length 2
    """

    child = serializers.ListField(child=serializers.CharField())

    def to_internal_value(self, data):
        if isinstance(data, str) and data:
            qs = parse_qs(data)
            result = []
            for key, values in qs.items():
                for value in values:
                    result.append([key, value])
            return result
        elif isinstance(data, dict):
            return [[key, value] for key, value in data.items()]
        elif isinstance(data, list):
            result = []
            for item in data:
                if isinstance(item, list) and len(item) >= 2:
                    result.append(item[:2])
            return result
        return None


class RequestSerializer(serializers.Serializer):
    env = serializers.DictField(
        child=serializers.CharField(allow_blank=True, allow_null=True), required=False
    )
    # Dict values can be both str and List[str]
    headers = serializers.DictField(required=False)
    url = serializers.CharField(required=False, allow_blank=True)
    method = serializers.CharField(required=False, allow_blank=True)
    query_string = QueryStringField(required=False, allow_null=True)


class ForgivingDisallowRegexField(ForgivingFieldMixin, serializers.CharField):
    """Disallow bad matches, set disallow_regex kwarg to use"""

    def __init__(self, **kwargs):
        self.disallow_regex = kwargs.pop("disallow_regex", None)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        if self.disallow_regex:
            pattern = re.compile(self.disallow_regex)
            if pattern.match(data) is None:
                error = ErrorValueDetail(
                    "invalid characters in string", "invalid_data", data
                )
                self.update_handled_errors_context([error])
                return None
        return data


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


class TransactionGroupSerializer(serializers.ModelSerializer):
    avgDuration = serializers.IntegerField(source="avg_duration", read_only=True)
    transactionCount = serializers.IntegerField(
        source="transaction_count", read_only=True
    )

    class Meta:
        model = TransactionGroup
        fields = [
            "id",
            "transaction",
            "project",
            "op",
            "method",
            "avgDuration",
            "transactionCount",
        ]


class SpanSerializer(serializers.ModelSerializer):
    spanId = serializers.CharField(source="span_id", read_only=True)
    parentSpanId = serializers.CharField(source="parent_span_id", read_only=True)
    startTimestamp = serializers.DateTimeField(source="start_timestamp", read_only=True)
    start_timestamp = FlexibleDateTimeField(write_only=True)
    timestamp = FlexibleDateTimeField(write_only=True)
    description = serializers.CharField(required=False)

    class Meta:
        model = Span
        fields = [
            "spanId",
            "span_id",
            "parent_span_id",
            "parentSpanId",
            "op",
            "description",
            "startTimestamp",
            "start_timestamp",
            "timestamp",
            "tags",
            "data",
        ]
        extra_kwargs = {
            "start_timestamp": {"write_only": True},
            "span_id": {"write_only": True},
            "parent_span_id": {"write_only": True},
        }

    def to_internal_value(self, data):
        # Coerce tags to strings
        # Must be done here to avoid failing child CharField validation
        if tags := data.get("tags"):
            data["tags"] = {key: str(value) for key, value in tags.items()}
        return super().to_internal_value(data)

    def validate_description(self, value):
        # No documented max length here, so we truncate
        max_length = self.Meta.model._meta.get_field("description").max_length
        if value and len(value) > max_length:
            logger.warning("Span description truncation %s", value)
            return value[:max_length]
        return value


class TransactionEventSerializer(SentrySDKEventSerializer):
    type = serializers.CharField(required=False)
    contexts = serializers.JSONField()
    measurements = serializers.JSONField(required=False)
    spans = serializers.ListField(
        child=SpanSerializer(), required=False, allow_empty=True
    )
    start_timestamp = FlexibleDateTimeField()
    timestamp = FlexibleDateTimeField()
    transaction = serializers.CharField()

    def create(self, validated_data):
        data = validated_data
        contexts = data["contexts"]
        project = self.context.get("project")
        trace_id = contexts["trace"]["trace_id"]

        tags = []
        release = self.set_release(data.get("release"), project)
        if project.release_id:
            tags.append(("release", release))
        environment = self.set_environment(data.get("environment"), project)
        if project.environment_id:
            tags.append(("environment", environment))

        if data.get("tags"):
            tags += [(k, v) for k, v in data["tags"].items()]

        defaults = {}
        defaults["tags"] = {tag[0]: [tag[1]] for tag in tags}

        group, group_created = TransactionGroup.objects.get_or_create(
            project=self.context.get("project"),
            transaction=data["transaction"],
            op=contexts["trace"].get("op", ""),
            method=data.get("request", {}).get("method"),
            defaults=defaults,
        )

        # Merge tags, only save if necessary
        update_group = False
        if not group_created:
            for tag in tags:
                if tag[0] not in group.tags:
                    new_tag_value = tag[1]
                    # Coerce to List[str]
                    if isinstance(new_tag_value, str):
                        new_tag_value = [new_tag_value]
                    group.tags[tag[0]] = new_tag_value
                    update_group = True
                elif tag[1] not in group.tags[tag[0]]:
                    group.tags[tag[0]].append(tag[1])
                    update_group = True
        if update_group:
            group.save(update_fields=["tags"])

        transaction = TransactionEvent.objects.create(
            group=group,
            data={
                "request": data.get("request"),
                "sdk": data.get("sdk"),
                "platform": data.get("platform"),
            },
            trace_id=trace_id,
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            start_timestamp=data["start_timestamp"],
            duration=(data["timestamp"] - data["start_timestamp"]).total_seconds()
            * 1000,
            tags={tag[0]: tag[1] for tag in tags},
        )

        first_span = SpanSerializer(
            data=contexts["trace"]
            | {
                "start_timestamp": data["start_timestamp"],
                "timestamp": data["timestamp"],
            }
        )
        if settings.ENABLE_PERFORMANCE_SPANS:
            is_valid = first_span.is_valid()
            if is_valid:
                spans = data.get("spans", []) + [first_span.validated_data]
            else:
                spans = data.get("spans")
            if spans:
                Span.objects.bulk_create(
                    [
                        Span(
                            transaction=transaction,
                            **span,
                        )
                        for span in spans
                    ]
                )

        return transaction


class TransactionSerializer(serializers.ModelSerializer):
    eventId = serializers.UUIDField(source="pk")
    startTimestamp = serializers.DateTimeField(source="start_timestamp")
    transaction = serializers.SerializerMethodField()
    op = serializers.SerializerMethodField()
    method = serializers.SerializerMethodField()

    class Meta:
        model = TransactionEvent
        fields = (
            "eventId",
            "timestamp",
            "startTimestamp",
            "transaction",
            "op",
            "method",
        )

    def get_transaction(self, obj):
        return obj.group.transaction

    def get_op(self, obj):
        return obj.group.op

    def get_method(self, obj):
        return obj.group.transaction


class TransactionDetailSerializer(TransactionSerializer):
    spans = SpanSerializer(source="span_set", many=True)

    class Meta(TransactionSerializer.Meta):
        fields = TransactionSerializer.Meta.fields + ("spans",)
