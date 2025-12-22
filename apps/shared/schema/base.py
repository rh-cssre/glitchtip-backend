from ninja import Schema
from pydantic import ConfigDict


class LaxIngestSchema(Schema):
    """Schema configuration for all event ingest schemas"""

    model_config = ConfigDict(coerce_numbers_to_str=True)
