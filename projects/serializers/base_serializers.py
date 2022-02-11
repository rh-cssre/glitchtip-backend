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


class ProjectReferenceWithMemberSerializer(ProjectReferenceSerializer):
    isMember = serializers.SerializerMethodField()

    class Meta(ProjectReferenceSerializer.Meta):
        fields = ProjectReferenceSerializer.Meta.fields + ("isMember",)

    def get_isMember(self, obj):
        user_id = self.context["request"].user.id
        teams = obj.team_set.all()
        # This is actually more performant than:
        # return obj.team_set.filter(members=user).exists()
        for team in teams:
            if user_id in team.members.all().values_list("user_id", flat=True):
                return True
        return False
