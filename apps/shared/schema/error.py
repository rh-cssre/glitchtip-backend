from typing import Any

from ninja import Schema


class EventProcessingError(Schema):
    """
    Represents a single error encountered during event processing,
    matching the Sentry event schema.
    """

    type: str
    name: str | None = None
    value: Any | None = None
