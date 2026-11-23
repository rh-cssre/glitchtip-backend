from datetime import datetime
from typing import Any

from ninja import Field, Schema
from pydantic import ConfigDict, RootModel

from glitchtip.schema import CamelSchema, to_camel


class ChecksumSchema(Schema):
    name: str | None = None
    debug_id: str | None = None
    chunks: list[str] = Field(default_factory=list)


class AssemblePayload(RootModel):
    root: dict[str, ChecksumSchema]


class DebugFileSchema(CamelSchema):
    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )

    id: str
    uuid: str | None = None
    debug_id: str | None = Field(None, alias="debugId")
    cpu_name: str
    object_name: str
    symbol_type: str | None = None
    size: int
    sha1: str
    date_created: datetime
    headers: dict[str, str]
    data: dict[str, Any]

    @staticmethod
    def resolve_id(obj):
        return str(obj.id)

    @staticmethod
    def resolve_uuid(obj):
        return obj.data.get("debug_id") if obj.data else None

    @staticmethod
    def resolve_debug_id(obj):
        return obj.data.get("debug_id") if obj.data else None

    @staticmethod
    def resolve_cpu_name(obj):
        return (obj.data.get("arch") or "any") if obj.data else "any"

    @staticmethod
    def resolve_object_name(obj):
        return obj.name

    @staticmethod
    def resolve_symbol_type(obj):
        return obj.data.get("symbol_type") if obj.data else None

    @staticmethod
    def resolve_size(obj):
        return obj.file.size

    @staticmethod
    def resolve_sha1(obj):
        return obj.file.checksum

    @staticmethod
    def resolve_date_created(obj):
        return obj.created

    @staticmethod
    def resolve_headers(obj):
        return obj.file.headers or {}

    @staticmethod
    def resolve_data(obj):
        return {"features": obj.data.get("features", [])} if obj.data else {}
