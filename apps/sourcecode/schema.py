from datetime import datetime
from typing import Annotated, Literal

from ninja import Schema
from pydantic import ConfigDict, Field

from glitchtip.schema import CamelSchema

HexField = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}$")]


class ArtifactBundleAssembleIn(Schema):
    checksum: HexField
    chunks: list[HexField]
    projects: list[str]
    version: str | None = None


AssembleState = Literal["created", "error", "not_found", "assembling", "ok"]


class AssembleResponse(Schema):
    state: AssembleState


class DebugSymbolBundleSchema(CamelSchema):
    id: str
    created: datetime = Field(serialization_alias="dateCreated")
    sha1: str | None = Field(validation_alias="file.checksum", default=None)
    headers: dict[str, str] | None = Field(
        validation_alias="file.headers", default=None
    )
    name: str = Field(validation_alias="file.name")

    model_config = ConfigDict(coerce_numbers_to_str=True)
