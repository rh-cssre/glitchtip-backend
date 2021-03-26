from rest_framework import serializers
from .models import ProjectAlert, AlertRecipient


class AlertRecipientSerializer(serializers.ModelSerializer):
    recipientType = serializers.CharField(source="recipientType")

    class Meta:
        model = AlertRecipient
        fields = ("pk", "recipientType", "url")


class ProjectAlertSerializer(serializers.ModelSerializer):
    alertRecipients = AlertRecipientSerializer(
        source="alertrecipient_set", many=True, required=False
    )

    class Meta:
        model = ProjectAlert
        fields = ("pk", "timespan_minutes", "quantity", "alertRecipients")

    def create(self, validated_data):
        import ipdb

        ipdb.set_trace()
        pass

    def update(self, instance, validated_data):
        import ipdb

        ipdb.set_trace()
        alert_recipients = validated_data.pop("alertRecipients")
        instance = super().update(instance, validated_data)
        return instance
