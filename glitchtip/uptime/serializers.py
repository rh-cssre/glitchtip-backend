from urllib.parse import urlparse

from django.conf import settings
from django.core.validators import URLValidator
from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.fields import ChoiceField

from .constants import HTTP_MONITOR_TYPES
from .models import Monitor, MonitorCheck, MonitorType, StatusPage


class MonitorCheckRelatedSerializer(serializers.ModelSerializer):
    isUp = serializers.BooleanField(source="is_up")
    startCheck = serializers.DateTimeField(source="start_check")

    class Meta:
        model = MonitorCheck
        fields = ("isUp", "startCheck", "reason")


class MonitorCheckSerializer(MonitorCheckRelatedSerializer):
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
    expectedStatus = serializers.IntegerField(source="expected_status", allow_null=True)
    expectedBody = serializers.CharField(source="expected_body", allow_blank=True)
    heartbeatEndpoint = serializers.SerializerMethodField()
    projectName = serializers.SerializerMethodField()
    envName = serializers.SerializerMethodField()
    checks = MonitorCheckRelatedSerializer(many=True, read_only=True)

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
            "expectedBody",
            "environment",
            "project",
            "organization",
            "interval",
            "isUp",
            "lastChange",
            "heartbeatEndpoint",
            "projectName",
            "envName",
            "timeout",
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

    def validate(self, attrs):
        monitor_type = attrs["monitor_type"]
        if attrs["url"] == "" and monitor_type in HTTP_MONITOR_TYPES + (
            MonitorType.SSL,
        ):
            raise serializers.ValidationError("URL is required for " + monitor_type)

        if monitor_type in HTTP_MONITOR_TYPES:
            URLValidator()(attrs["url"])

        if attrs.get("expected_status") is None and monitor_type in [
            MonitorType.GET,
            MonitorType.POST,
        ]:
            raise serializers.ValidationError(
                "Expected status is required for " + attrs["monitor_type"]
            )

        if attrs["monitor_type"] == MonitorType.PORT:
            url = attrs["url"].replace("http://", "//", 1)
            if not url.startswith("//"):
                url = "//" + url
            parsed_url = urlparse(url)
            message = "Invalid Port URL, expected hostname and port"
            try:
                if not all([parsed_url.hostname, parsed_url.port]):
                    raise serializers.ValidationError(message)
            except ValueError as err:
                raise serializers.ValidationError(message) from err
            attrs["url"] = f"{parsed_url.hostname}:{parsed_url.port}"

        return attrs


class MonitorDetailSerializer(MonitorSerializer):
    checks = MonitorCheckSerializer(many=True, read_only=True)


class StatusPageSerializer(serializers.ModelSerializer):
    isPublic = serializers.BooleanField(source="is_public")

    class Meta:
        model = StatusPage
        fields = ("name", "slug", "isPublic", "monitors")
        read_only_fields = ("slug",)
