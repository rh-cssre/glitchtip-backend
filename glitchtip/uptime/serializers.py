from django.utils import timezone
from rest_framework import serializers

from .models import Monitor, MonitorCheck


class HeartBeatCheckSerializer(serializers.ModelSerializer):
    start_check = serializers.DateTimeField(
        default=timezone.now, help_text="Optional, set server check start time."
    )

    class Meta:
        model = MonitorCheck
        fields = ("is_up", "start_check")
        read_only_fields = ("is_up",)


class MonitorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitor
        fields = (
            "id",
            "monitor_type",
            "endpoint_id",
            "created",
            "name",
            "url",
            "expected_status",
            "expected_body",
            "ip_address",
            "environment",
            "project",
            "organization",
            "interval",
        )
