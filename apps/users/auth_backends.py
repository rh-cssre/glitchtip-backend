from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend as DjangoModelBackend
from django.views.decorators.debug import sensitive_variables


class ModelBackend(DjangoModelBackend):
    """ModelBackend with the user-enumeration timing mitigation off the event loop.

    Django 6.0's ``ModelBackend.aauthenticate`` runs the dummy password hash
    for nonexistent users synchronously, stalling the event loop for the full
    hash duration (roughly 200ms) on every login attempt with an unknown
    email. Django 6.1 offloads it (ticket #36901); this override does the
    same and is redundant, but harmless, once the Django pin moves past 6.0.
    """

    @sensitive_variables("password")
    async def aauthenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)
        if username is None or password is None:
            return None
        try:
            user = await user_model._default_manager.aget_by_natural_key(username)
        except user_model.DoesNotExist:
            # Burn a hash so unknown and known users take the same time,
            # in a thread so the loop keeps serving other requests.
            await sync_to_async(user_model().set_password, thread_sensitive=False)(
                password
            )
        else:
            if await user.acheck_password(password) and self.user_can_authenticate(
                user
            ):
                return user
        return None
