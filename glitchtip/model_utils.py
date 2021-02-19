from django.db import models


class FromStringIntegerChoices(models.IntegerChoices):
    @classmethod
    def from_string(cls, string: str):
        for status in cls:
            if status.label == string:
                return status
