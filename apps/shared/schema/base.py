from ninja import Schema


class LaxIngestSchema(Schema):
    """Schema configuration for all event ingest schemas"""

    class Config(Schema.Config):
        coerce_numbers_to_str = True  # Lax is best for ingest
