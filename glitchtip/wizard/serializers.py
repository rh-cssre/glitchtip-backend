from rest_framework import serializers

from api_tokens.serializers import APITokenSerializer
from projects.serializers.serializers import ProjectWithKeysSerializer


class SetupWizardSerializer(serializers.Serializer):
    hash = serializers.CharField(max_length=64, min_length=64)


class SetupWizardResultSerializer(serializers.Serializer):
    apiKeys = APITokenSerializer()
    projects = ProjectWithKeysSerializer(many=True)
