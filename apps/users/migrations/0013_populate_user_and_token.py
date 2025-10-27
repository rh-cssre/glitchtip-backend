import os
from django.conf import settings
from django.db import migrations
from apps.users.models import User


def provision_user_and_token(apps, schema_editor):
    email = os.getenv("INITIAL_USER_EMAIL")
    password = os.getenv("INITIAL_USER_PASSWORD")
    token = os.getenv("INITIAL_USER_AUTH_TOKEN")

    if email is not None and password is not None:
        superuser = User.objects.create_superuser(
            email=email,
            password=password,
        )

        if token is not None:
            APIToken = apps.get_model("api_tokens", "APIToken")
            APIToken.objects.create(
                scopes=getattr(APIToken.scopes, "org:admin"),
                user_id=superuser.id,
                token=token,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0011_alter_user_email"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        migrations.swappable_dependency("api_tokens.APIToken"),
    ]
    operations = [
        migrations.RunPython(provision_user_and_token),
    ]
