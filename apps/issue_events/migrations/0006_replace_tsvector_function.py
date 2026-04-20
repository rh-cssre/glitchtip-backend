from django.db import migrations
from django.db.migrations import RunSQL
from apps.shared.migration_utils import get_sql_content


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0005_issue_issue_title_trgm_idx"),
    ]

    operations = [
        RunSQL(
            sql=get_sql_content(__file__, "append_and_limit_tsvector.sql"),
            reverse_sql="DROP FUNCTION IF EXISTS append_and_limit_tsvector(tsvector, TEXT, INTEGER, REGCONFIG);",
        )
    ]
