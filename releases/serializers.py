from django.db import IntegrityError
from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceSerializer
from files.models import File
from glitchtip.exceptions import ConflictException
from .models import Release, ReleaseFile


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

    def create(self, validated_data):
        version = validated_data.pop("version")
        organization = validated_data.pop("organization")
        instance, _ = Release.objects.get_or_create(
            version=version, organization=organization, defaults=validated_data
        )
        return instance


class ReleaseUpdateSerializer(ReleaseSerializer):
    version = serializers.CharField(read_only=True)


class ReleaseFileSerializer(serializers.ModelSerializer):
    file = serializers.FileField(write_only=True, allow_empty_file=True)
    sha1 = serializers.CharField(source="file.checksum", read_only=True)
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    headers = serializers.JSONField(source="file.headers", read_only=True)
    size = serializers.IntegerField(source="file.size", read_only=True)

    id = serializers.CharField(read_only=True)

    class Meta:
        model = ReleaseFile
        fields = ("sha1", "name", "dateCreated", "headers", "file", "id", "size")
        read_only_fields = ("headers",)
        extra_kwargs = {"file": {"write_only": True}}

    def create(self, validated_data):
        fileobj = validated_data.pop("file")
        release = validated_data.pop("release")
        full_name = validated_data.get("name", fileobj.name)
        name = full_name.rsplit("/", 1)[-1]
        headers = {"Content-Type": fileobj.content_type}

        validated_data["name"] = name
        validated_data["headers"] = headers

        file = File.objects.create(name=name, headers=headers)
        file.putfile(fileobj)

        try:
            release_file = ReleaseFile.objects.create(
                release=release, file=file, name=full_name,
            )
        except IntegrityError:
            file.delete()
            raise ConflictException("File already present!")

        return release_file


class AssembleSerializer(serializers.Serializer):
    checksum = serializers.RegexField("^[a-fA-F0-9]+$", max_length=40, min_length=40)
    chunks = serializers.ListField(
        child=serializers.RegexField("^[a-fA-F0-9]+$", max_length=40, min_length=40)
    )

