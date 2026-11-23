from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("issue_events", "0012_remove_issueevent_received"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="tagvalue",
                    name="value",
                    field=models.CharField(max_length=255),
                ),
                migrations.AddConstraint(
                    model_name="tagvalue",
                    constraint=models.UniqueConstraint(
                        fields=("value",),
                        name="issue_events_tagvalue_unique_value",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "DROP INDEX CONCURRENTLY IF EXISTS issue_events_tagvalue_value_998613e9_like;",
                        "ALTER INDEX issue_events_tagvalue_value_key RENAME TO issue_events_tagvalue_unique_value;",
                    ],
                    reverse_sql=[
                        "ALTER INDEX issue_events_tagvalue_unique_value RENAME TO issue_events_tagvalue_value_key;",
                        (
                            "CREATE INDEX CONCURRENTLY issue_events_tagvalue_value_998613e9_like "
                            "ON issue_events_tagvalue USING btree (value varchar_pattern_ops);"
                        ),
                    ],
                ),
            ],
        ),
    ]
