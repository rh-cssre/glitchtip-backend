import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger(__name__)

PARTMAN_CONFIG = {
    "issues_issueaggregate": {
        "control_column": "date",
        "interval": "2 days",  # Updated interval
        "premake": 3,  # Keep 3 * 2 days = 6 days of future partitions
        "retention": "14 days",
        "offset_days": 0,  # No offset, creates partitions on day 0, 2, 4...
    },
    # "events_event": {
    #     "control_column": "created_at",
    #     "interval": "2 days",
    #     "premake": 4,
    #     "retention": "90 days",
    #     "offset_days": 1,      # Offset by 1 day, creates partitions on day 1, 3, 5...
    # },
}


class Command(BaseCommand):
    help = (
        "Sets up and configures pg_partman for all tables defined in the PARTMAN_CONFIG. "
        "This command is idempotent and requires SUPERUSER privileges."
    )

    def handle(self, *args, **options):
        if not settings.GLITCHTIP_ADVANCED_PARTITIONING:
            self.stdout.write(
                self.style.WARNING(
                    "GLITCHTIP_ADVANCED_PARTITIONING is not enabled. Skipping."
                )
            )
            return

        self.stdout.write("Configuring pg_partman for advanced partitioned tables...")

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    # Ensure the extension is enabled.
                    self.stdout.write("--> Ensuring pg_partman extension exists...")
                    cursor.execute("CREATE SCHEMA IF NOT EXISTS partman;")
                    cursor.execute(
                        "CREATE EXTENSION IF NOT EXISTS pg_partman WITH SCHEMA partman;"
                    )

                    # Loop through each configured table and set it up.
                    for table_name, config in PARTMAN_CONFIG.items():
                        self.stdout.write(f"--> Configuring table: {table_name}")

                        # Loop through each of the HASH partitions (p0 to p3)
                        for i in range(4):
                            parent_partition_table = f"public.{table_name}_p{i}"
                            offset_interval = f"'{config['offset_days']} day'::interval"

                            # Use named arguments for clarity in the format string
                            sql = f"""
                                DO $$
                                DECLARE
                                    v_parent_table TEXT := %(parent_table)s;
                                BEGIN
                                    IF NOT EXISTS (
                                        SELECT 1 FROM partman.part_config
                                        WHERE parent_table = v_parent_table
                                    ) THEN
                                        PERFORM partman.create_parent(
                                            p_parent_table := v_parent_table,
                                            p_control := %(control)s,
                                            p_type := 'native',
                                            p_interval := %(interval)s,
                                            p_premake := %(premake)s,
                                            p_start_partition := (CURRENT_TIMESTAMP - {offset_interval})::text
                                        );
                                    END IF;
                                END;
                                $$;
                            """
                            cursor.execute(
                                sql,
                                {
                                    "parent_table": parent_partition_table,
                                    "control": config["control_column"],
                                    "interval": config["interval"],
                                    "premake": config["premake"],
                                },
                            )

                        # Set the retention policy for the entire partition set.
                        update_sql = """
                            UPDATE partman.part_config
                            SET
                                retention = %(retention)s,
                                retention_keep_table = false
                            WHERE parent_table LIKE %(parent_like)s;
                        """
                        cursor.execute(
                            update_sql,
                            {
                                "retention": config["retention"],
                                "parent_like": f"public.{table_name}_p%",
                            },
                        )
                        self.stdout.write(
                            f"    ... configuration for {table_name} applied."
                        )

            self.stdout.write(
                self.style.SUCCESS("Successfully configured all pg_partman tables.")
            )
            self.stdout.write(
                self.style.NOTICE(
                    "Remember to set up a cron job to call 'partman.run_maintenance_proc()' periodically."
                )
            )

        except Exception as e:
            logger.exception("An error occurred during pg_partman setup.")
            self.stderr.write(
                self.style.ERROR(
                    f"An error occurred: {e}\nAre you running this command as a database superuser?"
                )
            )
