from django.conf import settings

from psqlextra.backend.migrations.operations.add_default_partition import (
    PostgresAddDefaultPartition,
)


class TestDefaultPartition(PostgresAddDefaultPartition):
    """Create default partition only on test database"""

    def state_forwards(self, *args, **kwargs):
        if settings.TESTING:
            super().state_forwards(*args, **kwargs)

    def database_forwards(self, *args, **kwargs):
        if settings.TESTING:
            super().database_forwards(*args, **kwargs)

    def database_backwards(self, *args, **kwargs):
        if settings.TESTING:
            super().database_backwards(*args, **kwargs)
