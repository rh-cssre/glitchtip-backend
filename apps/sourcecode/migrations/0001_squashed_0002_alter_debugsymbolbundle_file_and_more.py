import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    replaces = [
        ("sourcecode", "0001_initial"),
        ("sourcecode", "0002_alter_debugsymbolbundle_file_and_more"),
    ]

    initial = True

    dependencies = [
        ("files", "0001_squashed_0009_alter_file_size"),
        ("organizations_ext", "0001_squashed_0008_merge_20250210_1625"),
        ("releases", "0001_squashed_0005_alter_releasefile_unique_together_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="DebugSymbolBundle",
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
                ("debug_id", models.UUIDField(blank=True, null=True)),
                ("last_used", models.DateTimeField(auto_now=True, db_index=True)),
                ("data", models.JSONField(default=dict)),
                (
                    "file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="files.file"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                (
                    "release",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="releases.release",
                    ),
                ),
                (
                    "sourcemap_file",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="files.file",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "debug_id"), name="unique_org_debug_id"
                    ),
                    models.UniqueConstraint(
                        fields=("release", "file"), name="unique_release_file"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("debug_id__isnull", False),
                            ("release__isnull", False),
                            _connector="OR",
                        ),
                        name="debug_id_or_release_required",
                    ),
                ],
            },
        ),
    ]
