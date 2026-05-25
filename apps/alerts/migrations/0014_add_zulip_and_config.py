from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0013_add_teams_recipient_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="alertrecipient",
            name="config",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name="alertrecipient",
            name="recipient_type",
            field=models.CharField(
                choices=[
                    ("email", "Email"),
                    ("webhook", "General Slack-compatible webhook"),
                    ("discord", "Discord"),
                    ("googlechat", "Google Chat webhook"),
                    ("ntfy", "ntfy"),
                    ("teams", "Microsoft Teams"),
                    ("zulip", "Zulip"),
                ],
                max_length=16,
            ),
        ),
    ]
