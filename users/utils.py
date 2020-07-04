from django.conf import settings
from organizations_ext.models import Organization


def is_user_registration_open() -> bool:
    enable_user_registration = settings.ENABLE_OPEN_USER_REGISTRATION
    if not enable_user_registration:
        enable_user_registration = not Organization.objects.exists()
    return enable_user_registration


def noop_token_creator(token_model, user, serializer):
    """ Fake token creator to use sessions instead of tokens """
    return None
