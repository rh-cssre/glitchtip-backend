import django.db.models.fields.json
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

INDEX = "difs_project_debug_id"


def drop_invalid_index(apps, schema_editor):
    """A CREATE INDEX CONCURRENTLY that was interrupted (the migration job
    killed mid-build, say) leaves an INVALID index behind, which would make
    the next run fail with "already exists". Drop it so the run can build
    it again."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s AND c.relnamespace = current_schema()::regnamespace "
            "AND NOT i.indisvalid",
            [INDEX],
        )
        invalid = cursor.fetchone() is not None
    if invalid:
        schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX}"')


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run in a transaction; it builds the
    # index without blocking uploads or ingest.
    atomic = False

    dependencies = [
        ("difs", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                AddIndexConcurrently(
                    model_name="debuginformationfile",
                    index=models.Index(
                        models.F("project"),
                        django.db.models.fields.json.KeyTransform("debug_id", "data"),
                        name=INDEX,
                    ),
                ),
            ],
            # The SQL AddIndexConcurrently runs, made safe to run again.
            database_operations=[
                migrations.RunPython(drop_invalid_index, migrations.RunPython.noop),
                migrations.RunSQL(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{INDEX}" ON '
                    '"difs_debuginformationfile" '
                    '("project_id", (("data" -> \'debug_id\')))',
                    reverse_sql=f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX}"',
                ),
            ],
        ),
    ]
