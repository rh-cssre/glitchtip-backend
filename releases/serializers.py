from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceSerializer
from .models import Release


class ReleaseSerializer(serializers.ModelSerializer):
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    shortVersion = serializers.CharField(source="version", read_only=True)
    deployCount = serializers.IntegerField(source="deploy_count", read_only=True)
    projects = ProjectReferenceSerializer(many=True, read_only=True)

    class Meta:
        model = Release
        fields = (
            "url",
            "data",
            "deployCount",
            "dateCreated",
            "version",
            "shortVersion",
            "projects",
        )
        lookup_field = "version"

    def create(self, validated_data):
        version = validated_data.pop("version")
        organization = validated_data.pop("organization")
        instance, _ = Release.objects.get_or_create(
            version=version, organization=organization, defaults=validated_data
        )
        return instance


class AssembleSerializer(serializers.Serializer):
    checksum = serializers.RegexField("^[a-fA-F0-9]+$", max_length=40, min_length=40)
    chunks = serializers.ListField(
        child=serializers.RegexField("^[a-fA-F0-9]+$", max_length=40, min_length=40)
    )

