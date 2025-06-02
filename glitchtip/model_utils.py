from enum import StrEnum
from typing import Union

from django.conf import settings
from django.db import models
from psql_partition.backend.migrations.operations.add_default_partition import (
    PostgresAddDefaultPartition,
)


class FromStringIntegerChoices(models.IntegerChoices):
    @classmethod
    def from_string(cls, string: Union[str, StrEnum]):
        for status in cls:
            if status.label == string:
                return status


class TestDefaultPartition(PostgresAddDefaultPartition):
    """Create default partition only on test database"""

    def is_testing_database(self) -> bool:
        return settings.TESTING or settings.ENABLE_TEST_API

    def state_forwards(self, *args, **kwargs):
        if self.is_testing_database():
            super().state_forwards(*args, **kwargs)

    def database_forwards(self, *args, **kwargs):
        if self.is_testing_database():
            super().database_forwards(*args, **kwargs)

    def database_backwards(self, *args, **kwargs):
        if self.is_testing_database():
            super().database_backwards(*args, **kwargs)
