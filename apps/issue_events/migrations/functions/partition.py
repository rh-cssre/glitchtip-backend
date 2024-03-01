from django.core.management import call_command


def create_partitions(apps, schema_editor):
    call_command("pgpartition", yes=True)
