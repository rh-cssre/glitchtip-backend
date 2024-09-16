from ninja import Field, Schema
from pydantic import RootModel


class ChecksumSchema(Schema):
    name: str | None = None
    debug_id: str | None = None
    chunks: list[str] = Field(default_factory=list)


class AssemblePayload(RootModel):
    root: dict[str, ChecksumSchema]
