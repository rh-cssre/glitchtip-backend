from rest_framework import serializers
from .models import Team


class RelatedTeamSerializer(serializers.ModelSerializer):
    """ Less detailed team serializer intended for nested relations """

    id = serializers.CharField(read_only=True)

    class Meta:
        model = Team
        fields = (
            "id",
            "slug",
        )


class TeamSerializer(RelatedTeamSerializer):
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    isMember = serializers.SerializerMethodField()
    memberCount = serializers.SerializerMethodField()

    class Meta(RelatedTeamSerializer.Meta):
        fields = (
            "dateCreated",
            "id",
            "isMember",
            "memberCount",
            "slug",
        )

    def get_isMember(self, obj):
        user = self.context["request"].user
        return obj.members.filter(user=user).exists()

    def get_memberCount(self, obj):
        return obj.members.count()
