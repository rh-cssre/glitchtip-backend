import logging

from rest_framework import serializers

from events.serializers import SentrySDKEventSerializer
from glitchtip.serializers import FlexibleDateTimeField

from .models import Span, TransactionEvent, TransactionGroup

logger = logging.getLogger(__name__)


class TransactionGroupSerializer(serializers.ModelSerializer):
    avgDuration = serializers.DurationField(source="avg_duration", read_only=True)
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
    description = serializers.CharField()

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
        if environment := data.get("environment"):
            environment = self.get_environment(data["environment"], project)
            tags.append(("environment", environment.name))
        if release := data.get("release"):
            release = self.get_release(release, project)
            tags.append(("release", release.version))
        defaults = {}
        defaults["tags"] = {tag[0]: [tag[1]] for tag in tags}

        group, group_created = TransactionGroup.objects.get_or_create(
            project=self.context.get("project"),
            transaction=data["transaction"],
            op=contexts["trace"]["op"],
            method=data.get("request", {}).get("method"),
            defaults=defaults,
        )

        # Merge tags, only save if necessary
        update_group = False
        if not group_created:
            for tag in tags:
                if tag[0] not in group.tags:
                    group.tags[tag[0]] = tag[1]
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
            duration=data["timestamp"] - data["start_timestamp"],
            tags={tag[0]: tag[1] for tag in tags},
        )

        first_span = SpanSerializer(
            data=contexts["trace"]
            | {
                "start_timestamp": data["start_timestamp"],
                "timestamp": data["timestamp"],
            }
        )
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
