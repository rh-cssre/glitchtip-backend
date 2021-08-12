from django.contrib.auth.forms import _unicode_ci_compare
from django.contrib.auth import get_user_model
from dj_rest_auth.forms import AllAuthPasswordResetForm

UserModel = get_user_model()

"""
Overriding this because the original filtered out users who didn't have a usable
password. This includes social auth users, and we want to give them the option
to set a password if they want to disconnect.
"""


class PasswordSetAndResetForm(AllAuthPasswordResetForm):
    def get_users(self, email):
        email_field_name = UserModel.get_email_field_name()
        active_users = UserModel._default_manager.filter(
            **{"%s__iexact" % email_field_name: email, "is_active": True,}
        )
        return (
            u
            for u in active_users
            if _unicode_ci_compare(email, getattr(u, email_field_name))
        )
