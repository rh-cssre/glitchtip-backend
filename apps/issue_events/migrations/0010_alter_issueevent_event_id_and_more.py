# State-only migration to sync model with indexes created in 0007_storage_v2_events SQL
# These indexes already exist in the database, this just updates Django's model state.
# Note: Uses OLD long names that were in the original SQL file. Migration 0011 will
# rename them to shorter names.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0009_storage_v2_issue_tag"),
        ("organizations_ext", "0010_alter_organization_id"),
        ("releases", "0007_alter_release_files"),
    ]

    operations = [
        # State-only: indexes already exist in DB from create_events_v2.sql
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="issueevent",
                    name="event_id",
                    field=models.UUIDField(
                        blank=True,
                        help_text="Client-provided event ID from Sentry SDK (UUIDv4)",
                        null=True,
                    ),
                ),
                # These use the OLD long names from original SQL file
                # Migration 0011 will rename them to shorter names
                migrations.AddIndex(
                    model_name="issueevent",
                    index=models.Index(
                        fields=["release"],
                        name="issue_events_issueevent_release_id_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="issueevent",
                    index=models.Index(
                        condition=models.Q(event_id__isnull=False),
                        fields=["event_id"],
                        name="issue_events_issueevent_event_id_idx",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
