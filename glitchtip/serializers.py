from datetime import datetime

from django.utils.timezone import make_aware
from rest_framework import serializers


class FlexibleDateTimeField(serializers.DateTimeField):
    """Supports both DateTime and unix epoch timestamp"""

    def to_internal_value(self, value):
        try:
            return make_aware(datetime.fromtimestamp(float(value)))
        except (ValueError, TypeError):
            return super().to_internal_value(value)
