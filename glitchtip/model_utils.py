from enum import StrEnum
from typing import Union

from django.db import models


class FromStringIntegerChoices(models.IntegerChoices):
    @classmethod
    def from_string(cls, string: Union[str, StrEnum]):
        for status in cls:
            if status.label == string:
                return status
