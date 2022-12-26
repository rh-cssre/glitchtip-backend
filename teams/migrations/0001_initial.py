# Generated by Django 3.0.3 on 2020-02-28 17:52

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations_ext", "0001_squashed_0009_organization_scrub_ip_addresses"),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Team",
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
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("slug", models.SlugField()),
                ("members", models.ManyToManyField(to=settings.AUTH_USER_MODEL)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="teams",
                        to="organizations_ext.Organization",
                    ),
                ),
                ("projects", models.ManyToManyField(to="projects.Project")),
            ],
            options={
                "unique_together": {("slug", "organization")},
            },
        ),
    ]
