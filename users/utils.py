from django.conf import settings
from users.models import User


def is_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or not User.objects.exists()


def noop_token_creator(token_model, user, serializer):
    """Fake token creator to use sessions instead of tokens"""
    return None
