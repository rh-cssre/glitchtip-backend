from django.conf import settings
from users.models import User


def is_user_registration_open() -> bool:
    enable_user_registration = settings.ENABLE_USER_REGISTRATION
    if not enable_user_registration:
        enable_user_registration = not User.objects.exists()
    return enable_user_registration

def noop_token_creator(token_model, user, serializer):
    """ Fake token creator to use sessions instead of tokens """
    return None
