from django.core.management import call_command


def create_partitions(*args, **kwargs):
    call_command("pgpartition", yes=True)
