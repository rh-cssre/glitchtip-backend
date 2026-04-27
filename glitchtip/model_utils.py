from enum import StrEnum
from typing import Union

from django.db import migrations, models


class FromStringIntegerChoices(models.IntegerChoices):
    @classmethod
    def from_string(cls, string: Union[str, StrEnum]):
        for status in cls:
            if status.label == string:
                return status


class TestDefaultPartition(migrations.RunSQL):
    """Dummy replacement for PostgresAddDefaultPartition for migration compatibility"""

    def __init__(self, *args, **kwargs):
        super().__init__("SELECT 1;")
