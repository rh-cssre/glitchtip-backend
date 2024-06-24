from rest_framework import serializers

from apps.projects.serializers.base_serializers import ProjectReferenceSerializer

from .models import Release


class ReleaseSerializer(serializers.ModelSerializer):
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    dateReleased = serializers.DateTimeField(source="released", required=False)
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
            "dateReleased",
            "version",
            "shortVersion",
            "projects",
        )
        lookup_field = "version"


class AssembleSerializer(serializers.Serializer):
    checksum = serializers.RegexField("^[a-fA-F0-9]+$", max_length=40, min_length=40)
    chunks = serializers.ListField(
        child=serializers.RegexField("^[a-fA-F0-9]+$", max_length=40, min_length=40)
    )
