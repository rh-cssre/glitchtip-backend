from datetime import datetime
from typing import Annotated, Literal

from ninja import Schema
from ninja.errors import ValidationError
from pydantic import Field, model_validator


class StatsV2Schema(Schema):
    category: Literal["error", "transaction"]
    interval: Literal["1d", "1h", "1m"] | None = "1h"
    project: list[Annotated[int, Field(ge=-1)]] | None = None
    field: Literal["sum(quantity)", "sum(times_seen)"]
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate(self):
        series_quantity = (self.end - self.start).days
        if self.interval == "1h":
            series_quantity *= 24
        elif self.interval == "1m":
            series_quantity *= 1440

        if series_quantity > 1000:
            raise ValidationError([{"end": "Too many intervals"}])
        return self
