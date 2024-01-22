# Generated by Django 4.2.7 on 2023-11-21 13:03

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0008_alter_alertrecipient_recipient_type"),
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
                    ("googlechat", "Google Chat webhook"),
                ],
                max_length=16,
            ),
        ),
    ]
