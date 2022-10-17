from django.conf import settings
from organizations_ext.models import Organization


def is_organization_creation_open() -> bool:
    enable_organization_creation = settings.ENABLE_ORGANIZATION_CREATION
    if not enable_organization_creation:
        enable_organization_creation = not Organization.objects.exists()
    return enable_organization_creation
