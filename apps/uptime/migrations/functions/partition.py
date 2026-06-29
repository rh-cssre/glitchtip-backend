from django.core.management import call_command


def create_partitions(apps, schema_editor):
    """
    Create partitions for all partitioned models using Storage V2 logic.
    """
    import logging

    logger = logging.getLogger(__name__)

    try:
        call_command("maintain_partitions")
    except Exception as e:
        logger.error(f"Failed to maintain partitions: {e}")
        # In migrations, we might want to ignore errors if it's a dry run or similar
        # but generally maintenance should succeed.
