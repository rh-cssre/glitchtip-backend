from rest_framework import serializers
from organizations.models import Organization


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ("name", "slug", "created")
        read_only_fields = ("slug", "created")
