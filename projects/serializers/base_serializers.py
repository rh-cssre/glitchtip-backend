from rest_framework import serializers
from ..models import Project


class ProjectReferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("platform", "slug", "id", "name")

