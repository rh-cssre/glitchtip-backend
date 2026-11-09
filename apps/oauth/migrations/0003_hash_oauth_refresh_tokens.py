from django.db import migrations, models


class Migration(migrations.Migration):
    """Replace the plaintext refresh-token columns with a prefix plus digest.

    The rows that held the only copy of the plaintext used to be deleted here,
    forcing those clients to re-authorize. That delete is gone: every 6.x
    install has already run it, and a fresh install has no tokens.
    """

    dependencies = [
        ("oauth", "0002_allow_blank_client_secret"),
    ]

    operations = [
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
