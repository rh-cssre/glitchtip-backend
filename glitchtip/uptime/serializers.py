from django.core import exceptions
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
    isUp = serializers.SerializerMethodField()
    lastChange = serializers.SerializerMethodField()
    monitorType = serializers.CharField(source="monitor_type")
    expectedStatus = serializers.IntegerField(source="expected_status")

    def get_isUp(self, obj):
        try: 
            return obj.latest_is_up
        except:
            return None
    
    def get_lastChange(self, obj):
        try:
            if obj.last_change:
                return obj.last_change.isoformat().replace("+00:00", "Z")
        except:
            return None

# dateCreated = serializers.DateTimeField(source="created", read_only=True)

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
        )
        read_only_fields = ("organization",)
