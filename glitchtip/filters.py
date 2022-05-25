import re
from datetime import timedelta

from django.utils import timezone
from django_filters import rest_framework as filters
from django_filters.fields import IsoDateTimeField, RangeField
from django_filters.widgets import DateRangeWidget


# Workaround https://github.com/carltongibson/django-filter/issues/1403
class StartEndDateRangeWidget(DateRangeWidget):
    """
    A range widget that uses 'start' and 'end' query params
    """

    suffixes = ["start", "end"]

    def suffixed(self, name, suffix):
        return suffix


RELATIVE_TIME_REGEX = re.compile(r"now\s*\-\s*\d+\s*(m|h|d)\s*$")


class RelativeIsoDateTimeField(IsoDateTimeField):
    """
    Allow relative terms like now or now-1h. Only 0 or 1 subtraction operation is permitted.

    Accepts
    - now
    - - (subtraction)
    - m (minutes)
    - h (hours)
    - d (days)
    """

    def strptime(self, value, format):
        # Check for relative time, if panic just assume it's a datetime
        result = timezone.now()
        if value == "now":
            return result
        if RELATIVE_TIME_REGEX.match(value):
            spaces_stripped = value.replace(" ", "")
            numbers = int(re.findall(r"\d+", spaces_stripped)[0])
            if spaces_stripped[-1] == "m":
                result -= timedelta(minutes=numbers)
            if spaces_stripped[-1] == "h":
                result -= timedelta(hours=numbers)
            if spaces_stripped[-1] == "d":
                result -= timedelta(days=numbers)
            return result
        return super().strptime(value, format)


class StartEndRelativeIsoDateTimeRangeField(RangeField):
    widget = StartEndDateRangeWidget

    def __init__(self, *args, **kwargs):
        fields = (RelativeIsoDateTimeField(), RelativeIsoDateTimeField())
        super().__init__(fields, *args, **kwargs)


class StartEndRelativeIsoDateTimeRangeFilter(filters.IsoDateTimeFromToRangeFilter):
    field_class = StartEndRelativeIsoDateTimeRangeField
