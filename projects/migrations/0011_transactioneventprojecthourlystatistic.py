# Generated by Django 4.1.4 on 2023-01-03 20:21

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0010_eventprojecthourlystatistic"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransactionEventProjectHourlyStatistic",
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
                ("date", models.DateTimeField()),
                ("count", models.PositiveIntegerField()),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "unique_together": {("project", "date")},
            },
        ),
    ]
