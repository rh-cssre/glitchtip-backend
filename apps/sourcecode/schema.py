from typing import Annotated, Literal

from ninja import Schema
from pydantic import Field

HexField = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}$")]


class ArtifactBundleAssembleIn(Schema):
    checksum: HexField
    chunks: list[HexField]
    projects: list[str]
    version: str


AssembleState = Literal["created", "error", "not_found", "assembling", "ok"]


class AssembleResponse(Schema):
    state: AssembleState
