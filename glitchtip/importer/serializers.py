from rest_framework import serializers
from organizations_ext.models import Organization, OrganizationUserRole


class ImportSerializer(serializers.Serializer):
    url = serializers.URLField()
    authToken = serializers.CharField()
    organizationSlug = serializers.SlugRelatedField(
        slug_field="slug", queryset=Organization.objects.none()
    )

    def __init__(self, context, *args, **kwargs):
        if user := context["request"].user:
            self.fields[
                "organizationSlug"
            ].queryset = user.organizations_ext_organization.filter(
                organization_users__role__gte=OrganizationUserRole.ADMIN
            )
        return super().__init__(*args, **kwargs)
