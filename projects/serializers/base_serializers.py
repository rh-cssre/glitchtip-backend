from rest_framework import serializers

from ..models import Project


class ProjectReferenceSerializer(serializers.ModelSerializer):
    """
    Non-detailed view used in:

    - /api/0/projects/<org-slug>/project-slug>/issues/
    """

    id = serializers.CharField(read_only=True)

    class Meta:
        model = Project
        fields = ("platform", "slug", "id", "name")
