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
