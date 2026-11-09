import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    replaces = [
        (
            "releases",
            "0001_squashed_0004_alter_release_id_alter_releasefile_id_and_more",
        ),
        ("releases", "0005_alter_releasefile_unique_together_and_more"),
    ]

    initial = True

    dependencies = [
        ("files", "0001_squashed_0009_alter_file_size"),
        ("organizations_ext", "0001_squashed_0008_merge_20250210_1625"),
        # ReleaseProject.project and the Release.projects through table both
        # reference projects_project, which leaf ordering happened to create
        # first; name it so the order is required rather than lucky.
        ("projects", "0001_squashed_0016_auto_20250125_1733"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReleaseFile",
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
                ("ident", models.CharField(max_length=40)),
                ("name", models.TextField()),
            ],
        ),
        migrations.CreateModel(
            name="Release",
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
                ("version", models.CharField(max_length=255)),
                (
                    "ref",
                    models.CharField(
                        blank=True,
                        help_text="May be branch or tag name",
                        max_length=255,
                        null=True,
                    ),
                ),
                ("url", models.URLField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("released", models.DateTimeField(blank=True, null=True)),
                ("data", models.JSONField(default=dict)),
                ("commit_count", models.PositiveSmallIntegerField(default=0)),
                ("deploy_count", models.PositiveSmallIntegerField(default=0)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        help_text="Release manager - the person initiating the release",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "projects",
                    models.ManyToManyField(
                        related_name="releases", to="projects.project"
                    ),
                ),
                (
                    "files",
                    models.ManyToManyField(
                        through="releases.ReleaseFile", to="files.file"
                    ),
                ),
            ],
            options={
                "unique_together": {("organization", "version")},
            },
        ),
        migrations.CreateModel(
            name="ReleaseProject",
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
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="projects.project",
                    ),
                ),
                (
                    "release",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="releases.release",
                    ),
                ),
            ],
            options={
                "unique_together": {("project", "release")},
            },
        ),
    ]
