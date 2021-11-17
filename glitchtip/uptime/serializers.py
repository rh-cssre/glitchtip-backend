from django.conf import settings
from django.core import exceptions
from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers

from .models import Monitor, MonitorCheck, MonitorType


class HeartBeatCheckSerializer(serializers.ModelSerializer):
    start_check = serializers.DateTimeField(
        default=timezone.now, help_text="Optional, set server check start time."
    )

    class Meta:
        model = MonitorCheck
        fields = ("is_up", "start_check")
        read_only_fields = ("is_up",)

# Taken from the issues serializer. Wasn't sure if I should import it or copy it.
class DisplayChoiceField(serializers.ChoiceField):
    """
    ChoiceField that represents choice only as display value
    Useful if the API should only deal with display values
    """

    def to_representation(self, value):
        return self.choices[value]

    def to_internal_value(self, data):
        if data == "" and self.allow_blank:
            return ""

        choice_strings_to_values = {value: key for key, value in self.choices.items()}
        try:
            return choice_strings_to_values[str(data)]
        except KeyError:
            self.fail("invalid_choice", input=data)

class MonitorSerializer(serializers.ModelSerializer):
    isUp = serializers.SerializerMethodField()
    lastChange = serializers.SerializerMethodField()
    monitorType = DisplayChoiceField(choices=MonitorType.choices, source="monitor_type")
    expectedStatus = serializers.IntegerField(source="expected_status")
    heartbeatEndpoint = serializers.SerializerMethodField()
    projectName = serializers.SerializerMethodField()
    envName = serializers.SerializerMethodField()

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
            "name",
            "url",
            "expectedStatus",
            "expected_body",
            "ip_address",
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
