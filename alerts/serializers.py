from rest_framework import serializers
from .models import ProjectAlert


class ProjectAlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectAlert
        fields = ("pk", "timespan_minutes", "quantity")
