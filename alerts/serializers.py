from rest_framework import serializers
from .models import ProjectAlert, AlertRecipient


class AlertRecipientSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRecipient
        fields = ("pk", "recipient_type", "url")


class ProjectAlertSerializer(serializers.ModelSerializer):
    alertrecipient_set = AlertRecipientSerializer(many=True, required=False)

    class Meta:
        model = ProjectAlert
        fields = ("pk", "timespan_minutes", "quantity", "alertrecipient_set")
