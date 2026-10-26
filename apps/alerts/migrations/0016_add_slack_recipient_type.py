from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0015_add_feishu_recipient_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="alertrecipient",
            name="recipient_type",
            field=models.CharField(
                choices=[
                    ("email", "Email"),
                    ("webhook", "Generic webhook (Mattermost, Rocket.Chat, and more)"),
                    ("discord", "Discord"),
                    ("slack", "Slack"),
                    ("googlechat", "Google Chat webhook"),
                    ("ntfy", "ntfy"),
                    ("teams", "Microsoft Teams"),
                    ("zulip", "Zulip"),
                    ("feishu", "Feishu (Lark) webhook"),
                ],
                max_length=16,
            ),
        ),
    ]
