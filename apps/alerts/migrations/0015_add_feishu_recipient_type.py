from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0014_add_zulip_and_config"),
    ]

    operations = [
        migrations.AlterField(
            model_name="alertrecipient",
            name="recipient_type",
            field=models.CharField(
                choices=[
                    ("email", "Email"),
                    ("webhook", "General Slack-compatible webhook"),
                    ("discord", "Discord"),
                    ("feishu", "Feishu (Lark) webhook"),
                    ("googlechat", "Google Chat webhook"),
                    ("ntfy", "ntfy"),
                    ("teams", "Microsoft Teams"),
                    ("zulip", "Zulip"),
                ],
                max_length=16,
            ),
        ),
    ]
