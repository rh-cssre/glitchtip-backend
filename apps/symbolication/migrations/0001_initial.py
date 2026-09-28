import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("files", "0002_delete_fileblobindex"),
    ]

    operations = [
        migrations.CreateModel(
            name="SymbolicationCache",
            # Column order is chosen for alignment: 8-byte columns, then 4-,
            # then 2-byte, then variable length. Keep the foreign keys here
            # rather than at the end, where makemigrations puts them.
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "source_blob",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.DB_SET_NULL,
                        related_name="+",
                        to="files.fileblob",
                    ),
                ),
                (
                    "secondary_blob",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.DB_SET_NULL,
                        related_name="+",
                        to="files.fileblob",
                    ),
                ),
                (
                    "requested_by_organization_id",
                    models.BigIntegerField(blank=True, null=True),
                ),
                ("size", models.PositiveIntegerField(default=0)),
                (
                    "kind",
                    models.PositiveSmallIntegerField(
                        choices=[(1, "sourcemap"), (2, "symcache"), (3, "proguard")]
                    ),
                ),
                (
                    "state",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (1, "Pending"),
                            (2, "Building"),
                            (3, "Ready"),
                            (4, "Failed"),
                        ],
                        default=1,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("cache_key", models.TextField()),
                ("format_tag", models.TextField()),
                ("storage_name", models.TextField(blank=True, default="")),
                ("error", models.TextField(blank=True, default="")),
            ],
            options={
                "indexes": [
                    models.Index(
                        condition=models.Q(("state__in", [1, 2])),
                        fields=["requested_by_organization_id"],
                        name="symbolication_cache_pending",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "cache_key", "format_tag"),
                        name="symbolication_cache_unique_key",
                    )
                ],
            },
        ),
    ]
