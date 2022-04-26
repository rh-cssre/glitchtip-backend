from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.fields import ChoiceField

from .models import Monitor, MonitorCheck, MonitorType


class MonitorCheckSerializer(serializers.ModelSerializer):
    isUp = serializers.BooleanField(source="is_up")
    startCheck = serializers.DateTimeField(source="start_check")
    responseTime = serializers.DurationField(source="response_time")

    class Meta:
        model = MonitorCheck
        fields = ("isUp", "startCheck", "reason", "responseTime")


class HeartBeatCheckSerializer(MonitorCheckSerializer):
    start_check = serializers.DateTimeField(
        default=timezone.now, help_text="Optional, set server check start time."
    )

    class Meta(MonitorCheckSerializer.Meta):
        fields = ("is_up", "start_check")
        read_only_fields = ("is_up",)


class MonitorSerializer(serializers.ModelSerializer):
    isUp = serializers.SerializerMethodField()
    lastChange = serializers.SerializerMethodField()
    monitorType = ChoiceField(choices=MonitorType.choices, source="monitor_type")
    expectedStatus = serializers.IntegerField(source="expected_status")
    heartbeatEndpoint = serializers.SerializerMethodField()
    projectName = serializers.SerializerMethodField()
    envName = serializers.SerializerMethodField()
    checks = MonitorCheckSerializer(many=True, read_only=True)

    def get_isUp(self, obj):
        if hasattr(obj, "latest_is_up"):
            return obj.latest_is_up

    def get_lastChange(self, obj):
        if hasattr(obj, "last_change"):
            if obj.last_change:
                return obj.last_change.isoformat().replace("+00:00", "Z")

    def get_heartbeatEndpoint(self, obj):
        if obj.endpoint_id:
            return settings.GLITCHTIP_URL.geturl() + reverse(
                "heartbeat-check",
                kwargs={
                    "organization_slug": obj.organization.slug,
                    "endpoint_id": obj.endpoint_id,
                },
            )

    def get_projectName(self, obj):
        if obj.project:
            return obj.project.name

    def get_envName(self, obj):
        if obj.environment:
            return obj.environment.name

    class Meta:
        model = Monitor
        fields = (
            "id",
            "monitorType",
            "endpoint_id",
            "created",
            "checks",
            "name",
            "url",
            "expectedStatus",
            "expected_body",
            "environment",
            "project",
            "organization",
            "interval",
            "isUp",
            "lastChange",
            "heartbeatEndpoint",
            "projectName",
            "envName",
        )
        read_only_fields = (
            "organization",
            "isUp",
            "lastChange",
            "endpoint_id",
            "created",
            "heartbeatEndpoint",
            "projectName",
            "envName",
        )

    def validate(self, data):
        if data["url"] == "" and data["monitor_type"] in [
            MonitorType.GET,
            MonitorType.PING,
            MonitorType.POST,
            MonitorType.SSL,
        ]:
            raise serializers.ValidationError(
                "URL is required for " + data["monitor_type"]
            )
        return data
