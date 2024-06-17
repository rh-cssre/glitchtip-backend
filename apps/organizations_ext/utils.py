from django.conf import settings

from apps.organizations_ext.models import Organization


def is_organization_creation_open() -> bool:
    return settings.ENABLE_ORGANIZATION_CREATION or not Organization.objects.exists()
