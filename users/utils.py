from django.conf import settings
from organizations_ext.models import Organization


def is_user_registration_open() -> bool:
    enable_user_registration = settings.ENABLE_OPEN_USER_REGISTRATION
    if not enable_user_registration:
        enable_user_registration = not Organization.objects.exists()
    return enable_user_registration
