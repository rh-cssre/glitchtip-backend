import django.db.models.fields.json
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run in a transaction; it builds the
    # index without blocking uploads or ingest.
    atomic = False

    dependencies = [
        ("difs", "0001_initial"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="debuginformationfile",
            index=models.Index(
                models.F("project"),
                django.db.models.fields.json.KeyTransform("debug_id", "data"),
                name="difs_project_debug_id",
            ),
        ),
    ]
