import re
from datetime import datetime, timedelta
from typing import Annotated

from django.utils import timezone
from pydantic import BeforeValidator, Field

SlugStr = Annotated[str, Field(pattern=r"^[-a-zA-Z0-9_]+$", max_length=50)]

# RELATIVE_TIME_REGEX = re.compile(r"now[+-]\d+[mhd]")
RELATIVE_TIME_REGEX = re.compile(r"now\s*\-\s*\d+\s*(m|h|d)\s*$")


def parse_relative_datetime(value: str) -> datetime:
    """
    Allow relative terms like now or now-1h. Only 0 or 1 math operation is permitted.

    Accepts
    - now
    - + (addition)
    - - (subtraction)
    - m (minutes)
    - h (hours)
    - d (days)
    """
    if value == "now":
        return timezone.now()

    match = RELATIVE_TIME_REGEX.match(value)
    if match:
        now = timezone.now()
        stripped_value = value.replace(" ", "")
        sign = 1 if "+" in stripped_value else -1
        number = int(re.findall(r"\d+", stripped_value)[0])

        if "m" in stripped_value:
            return now + sign * timedelta(minutes=number)
        elif "h" in stripped_value:
            return now + sign * timedelta(hours=number)
        elif "d" in stripped_value:
            return now + sign * timedelta(days=number)

    return value


RelativeDateTime = Annotated[datetime, BeforeValidator(parse_relative_datetime)]
