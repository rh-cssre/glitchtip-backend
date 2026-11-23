import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    replaces = [
        ("alerts", "0001_squashed_0010_alter_notification_issues"),
        ("alerts", "0011_alertrecipient_tags_to_add"),
    ]

    initial = True

    dependencies = [
        ("issue_events", "0001_squashed_0006_replace_tsvector_function"),
        ("projects", "0001_squashed_0016_auto_20250125_1733"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectAlert",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "timespan_minutes",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("quantity", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="projects.project",
                    ),
                ),
                ("name", models.CharField(blank=True, max_length=255)),
                (
                    "uptime",
                    models.BooleanField(
                        default=False,
                        help_text="Send alert on any uptime monitor check failure",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Notification",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("is_sent", models.BooleanField(default=False)),
                ("issues", models.ManyToManyField(to="issue_events.issue")),
                (
                    "project_alert",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="alerts.projectalert",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AlertRecipient",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "recipient_type",
                    models.CharField(
                        choices=[
                            ("email", "Email"),
                            ("webhook", "General Slack-compatible webhook"),
                            ("discord", "Discord"),
                            ("googlechat", "Google Chat webhook"),
                        ],
                        max_length=16,
                    ),
                ),
                ("url", models.URLField(blank=True, max_length=2000)),
                (
                    "alert",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="alerts.projectalert",
                    ),
                ),
                (
                    "tags_to_add",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=255),
                        blank=True,
                        default=list,
                        help_text="List of additional tags to include in the alert",
                        null=True,
                    ),
                ),
            ],
            options={
                "unique_together": {("alert", "recipient_type", "url")},
            },
        ),
    ]
