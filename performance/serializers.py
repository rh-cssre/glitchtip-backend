from rest_framework import serializers

from events.serializers import SentrySDKEventSerializer
from glitchtip.serializers import FlexibleDateTimeField

from .models import Span, TransactionEvent, TransactionGroup


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


class TransactionEventSerializer(SentrySDKEventSerializer):
    type = serializers.CharField()
    contexts = serializers.JSONField()
    measurements = serializers.JSONField(required=False)
    spans = serializers.ListField(
        child=SpanSerializer(), required=False, allow_empty=True
    )
    start_timestamp = FlexibleDateTimeField()
    timestamp = FlexibleDateTimeField()
    transaction = serializers.CharField()

    def create(self, data):
        trace_id = data["contexts"]["trace"]["trace_id"]

        group, _ = TransactionGroup.objects.get_or_create(
            project=self.context.get("project"),
            transaction=data["transaction"],
            op=data["contexts"]["trace"]["op"],
            method=data.get("request", {}).get("method"),
        )
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
        )

        first_span = SpanSerializer(
            data=data["contexts"]["trace"]
            | {
                "start_timestamp": data["start_timestamp"],
                "timestamp": data["timestamp"],
            }
        )
        first_span.is_valid()
        spans = data.get("spans", []) + [first_span.validated_data]
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
