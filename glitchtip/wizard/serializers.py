from rest_framework import serializers


class SetupWizardSerializer(serializers.Serializer):
    hash = serializers.CharField(max_length=64, min_length=64)
