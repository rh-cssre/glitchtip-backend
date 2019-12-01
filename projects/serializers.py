from rest_framework import serializers
from .models import Project, ProjectKey


class ProjectKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectKey
        fields = ("label", "public_key", "date_added", "dsn")
        read_only_fields = ("date_added", "dsn")


class ProjectSerializer(serializers.ModelSerializer):
    projectkey_set = ProjectKeySerializer(many=True, required=False)

    class Meta:
        model = Project
        fields = ("name", "slug", "date_added", "platform", "projectkey_set")
        read_only_fields = ("slug", "date_added", "projectkey_set")

