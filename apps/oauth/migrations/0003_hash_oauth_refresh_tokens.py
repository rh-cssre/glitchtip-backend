from django.db import migrations, models


def truncate_refresh_tokens(apps, schema_editor):
    """Drop all existing refresh tokens before replacing the token columns.

    We're removing the plaintext ``token`` and ``access_token_key`` fields
    and replacing them with a prefix plus a SHA-256 digest. Since the old
    columns held the only copy of the plaintext that we could hash, we
    just delete the rows: connected clients will see the next access-token
    exchange fail and re-authorize via the OAuth flow, which is a
    one-time user-visible reconnection rather than a breakage.
    """
    OAuthRefreshToken = apps.get_model("oauth", "OAuthRefreshToken")
    OAuthRefreshToken.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("oauth", "0002_allow_blank_client_secret"),
    ]

    operations = [
        migrations.RunPython(truncate_refresh_tokens, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="oauthrefreshtoken",
            name="token",
        ),
        migrations.RemoveField(
            model_name="oauthrefreshtoken",
            name="access_token_key",
        ),
        migrations.AddField(
            model_name="oauthrefreshtoken",
            name="token_prefix",
            field=models.CharField(
                db_index=True,
                default="",
                help_text="First 8 chars of the plaintext refresh token, for lookup",
                max_length=8,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="oauthrefreshtoken",
            name="token_digest",
            field=models.CharField(
                default="",
                help_text="SHA-256 hex digest of the plaintext refresh token",
                max_length=64,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="oauthrefreshtoken",
            name="access_token_digest",
            field=models.CharField(
                default="",
                help_text="SHA-256 hex digest of the paired access token, for revocation",
                max_length=64,
            ),
            preserve_default=False,
        ),
    ]
