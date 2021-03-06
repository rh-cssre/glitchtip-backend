# Generated by Django 3.0.3 on 2020-04-17 20:38

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("projects", "0001_initial"),
        ("issues", "0001_squashed_0010_auto_20210117_1543"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectAlert",
            fields=[
                (
                    "id",
                    models.AutoField(
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
                ("created", models.DateTimeField(auto_now_add=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="projects.Project",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Notification",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateField(auto_now_add=True)),
                ("is_sent", models.BooleanField(default=False)),
                ("issues", models.ManyToManyField(to="issues.Issue")),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="projects.Project",
                    ),
                ),
            ],
        ),
    ]
