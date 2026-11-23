from django.db import migrations, models
from django.db.migrations import RunSQL

from apps.shared.migration_utils import get_sql_content


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0022_add_org_date_indexes_to_hourly_stats"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="scrub_config",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    "Server-side PII scrubbing config applied at ingest. See "
                    "apps.event_ingest.pii_scrubber.ScrubConfig for the accepted "
                    "keys (enabled, sensitive_keys, safe_keys, scrub_emails, ...). "
                    "Null falls back to the GLITCHTIP_PII_SCRUB_DEFAULT setting."
                ),
            ),
        ),
        # Recreate get_project_auth_info to also return the new scrub_config
        # column (the ingest hot path reads project auth via this function).
        # Each direction reads a version-pinned SQL file rather than the shared
        # get_project_auth_info.sql, which migration 0018 reads at runtime —
        # editing that shared file would retroactively change what 0018 emits.
        RunSQL(
            sql=get_sql_content(__file__, "get_project_auth_info_0023.sql"),
            reverse_sql=get_sql_content(__file__, "get_project_auth_info_0018.sql"),
        ),
    ]
