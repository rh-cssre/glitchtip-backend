# Generated by Django 4.2.1 on 2023-05-11 12:18

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "alerts",
            "0001_squashed_0007_alter_alertrecipient_id_alter_notification_id_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="alertrecipient",
            name="webhook_type",
            field=models.CharField(
                choices=[
                    ("discord", "Discord"),
                    ("slack", "Slack"),
                    ("microsoft_teams", "Microsoft Teams"),
                    ("rocket_chat", "Rocket.Chat"),
                    ("general_webhook", "General Webhook"),
                ],
                default="general_webhook",
                max_length=255,
            ),
        ),
    ]
