from rest_framework import serializers
from .models import ProjectAlert, AlertRecipient


class AlertRecipientSerializer(serializers.ModelSerializer):
    recipientType = serializers.CharField(source="recipient_type")

    class Meta:
        model = AlertRecipient
        fields = ("pk", "recipientType", "url")


class ProjectAlertSerializer(serializers.ModelSerializer):
    alertRecipients = AlertRecipientSerializer(
        source="alertrecipient_set", many=True, required=False
    )

    class Meta:
        model = ProjectAlert
        fields = (
            "pk",
            "name",
            "timespan_minutes",
            "quantity",
            "uptime",
            "alertRecipients",
        )

    def create(self, validated_data):
        alert_recipients = validated_data.pop("alertrecipient_set", [])
        instance = super().create(validated_data)
        for recipient in alert_recipients:
            instance.alertrecipient_set.create(**recipient)
        return instance

    def update(self, instance, validated_data):
        alert_recipients = validated_data.pop("alertrecipient_set", [])
        instance = super().update(instance, validated_data)

        # Create/Delete recipients as needed
        delete_recipient_ids = set(
            instance.alertrecipient_set.values_list("id", flat=True)
        )
        for recipient in alert_recipients:
            new_recipient, created = AlertRecipient.objects.get_or_create(
                alert=instance, **recipient
            )
            if not created:
                delete_recipient_ids.discard(new_recipient.pk)
        if delete_recipient_ids:
            instance.alertrecipient_set.filter(pk__in=delete_recipient_ids).delete()
        return instance
