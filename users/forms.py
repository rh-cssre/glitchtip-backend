from django.contrib.auth.forms import PasswordResetForm, _unicode_ci_compare
from django.contrib.auth import get_user_model

UserModel = get_user_model()

class PasswordSetAndResetForm(PasswordResetForm):
    def get_users(self, email):
        email_field_name = UserModel.get_email_field_name()
        active_users = UserModel._default_manager.filter(**{
            '%s__iexact' % email_field_name: email,
            'is_active': True,
        })
        return (
            u for u in active_users
            if _unicode_ci_compare(
                email, getattr(u, email_field_name)
            )
        )
