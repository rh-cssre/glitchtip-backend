import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.utils import ProgrammingError

logger = logging.getLogger(__name__)

# Define the configuration for all pg_partman managed tables in one place.
PARTMAN_CONFIG = {
    "issue_events_issueaggregate": {
        "control_column": "date",
        "interval": "2 days",
        "premake": 4,
        "retention": "14 days",
        "offset_days": 0,
    },
    "performance_transactionevent": {
        "control_column": "start_timestamp",
        "interval": "2 days",
        "premake": 4,
        "retention": "90 days",
        "offset_days": 1,
    },
    "performance_transactiongroupaggregate": {
        "control_column": "date",
        "interval": "2 days",
        "premake": 4,
        "retention": "90 days",
        "offset_days": 0,
    },
}

# The SQL script that an administrator needs to run if the command fails.
ADMIN_SETUP_SQL = """
-- Create the schema and the extension.
CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman WITH SCHEMA partman;

-- Grant USAGE on the schema to your application's user.
GRANT USAGE ON SCHEMA partman TO your_application_user;

-- Grant EXECUTE permission on all functions and procedures in the schema.
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO your_application_user;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO your_application_user;

-- Grant permissions on the pg_partman configuration tables.
GRANT ALL ON TABLE partman.part_config TO your_application_user;
GRANT ALL ON TABLE partman.part_config_sub TO your_application_user;

-- Grant permission for the user to create objects in the public schema.
GRANT CREATE ON SCHEMA public TO your_application_user;
"""


class Command(BaseCommand):
    help = (
        "Sets up and configures pg_partman for all tables defined in the PARTMAN_CONFIG. "
        "Will attempt to create the extension, but may require a superuser to run a manual SQL script if it fails."
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

        # --- Step 1: Check for and set up the pg_partman extension ---
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'partman');"
                )
                schema_exists = cursor.fetchone()[0]

                if not schema_exists:
                    self.stdout.write(
                        "--> 'partman' schema not found. Attempting to create schema and extension..."
                    )
                    # This block will work in local dev but fail on managed DBs if not run as admin.
                    with transaction.atomic():
                        cursor.execute("CREATE SCHEMA IF NOT EXISTS partman;")
                        cursor.execute(
                            "CREATE EXTENSION IF NOT EXISTS pg_partman WITH SCHEMA partman;"
                        )
                    self.stdout.write(
                        self.style.SUCCESS(
                            "    ... Schema and extension setup successful."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            "--> 'partman' schema already exists. Skipping creation."
                        )
                    )

        except ProgrammingError as e:
            # Catch the specific permission error and provide helpful guidance.
            if "permission denied" in str(e).lower():
                self.stderr.write(
                    self.style.ERROR(
                        "\nPermission denied. This command must be run by a user with rights to CREATE SCHEMA and CREATE EXTENSION."
                    )
                )
                self.stderr.write(
                    self.style.ERROR(
                        "This is common on managed database platforms like DigitalOcean or AWS RDS."
                    )
                )
                self.stdout.write(
                    "\n------------------------------------------------------------------"
                )
                self.stdout.write(self.style.SUCCESS("ACTION REQUIRED:"))
                self.stdout.write(
                    "Please ask your database administrator to run the following SQL script as a high-privilege user (e.g., 'doadmin'):"
                )
                self.stdout.write(
                    "\n-- Please replace 'your_application_user' with the correct user name before running! --"
                )
                self.stdout.write(
                    self.style.SQL_KEYWORD(
                        ADMIN_SETUP_SQL.replace(
                            "your_application_user", connection.settings_dict["USER"]
                        )
                    )
                )
                self.stdout.write(
                    "\nAfter the script is run, you can re-run this management command."
                )
                self.stdout.write(
                    "------------------------------------------------------------------"
                )
                return  # Halt execution if setup fails
            else:
                raise e  # Re-raise any other programming errors.

        # --- Step 2: Proceed with table configuration ---
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    for table_name, config in PARTMAN_CONFIG.items():
                        self.stdout.write(f"--> Configuring table: {table_name}")
                        for i in range(4):
                            parent_partition_table = f"public.{table_name}_p{i}"
                            offset_interval = f"'{config['offset_days']} day'::interval"
                            sql = f"""
                                DO $$
                                BEGIN
                                    IF NOT EXISTS (
                                        SELECT 1 FROM partman.part_config WHERE parent_table = %(parent_table)s
                                    ) THEN
                                        PERFORM partman.create_parent(
                                            p_parent_table := %(parent_table)s,
                                            p_control := %(control)s,
                                            p_type := 'range',
                                            p_interval := %(interval)s,
                                            p_premake := %(premake)s,
                                            p_start_partition := (CURRENT_TIMESTAMP - {offset_interval})::text,
                                            p_default_table := false
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
                        update_sql = "UPDATE partman.part_config SET retention = %(retention)s, retention_keep_table = false, infinite_time_partitions = true WHERE parent_table LIKE %(parent_like)s;"
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
            logger.exception("An error occurred during pg_partman table configuration.")
            self.stderr.write(
                self.style.ERROR(
                    f"An error occurred during table configuration: {e}\nHave the necessary permissions been granted to the 'partman' schema?"
                )
            )
