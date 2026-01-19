from typing import Annotated, Literal, Union
from urllib.parse import urlparse

from pydantic import WrapValidator, field_validator, model_validator

from ..schema.base import LaxIngestSchema
from ..schema.utils import invalid_to_none


class PosixSignal(LaxIngestSchema):
    number: int
    code: int | None
    name: str | None
    code_name: str | None


class MachException(LaxIngestSchema):
    number: int
    code: int
    subcode: int
    name: str | None


class NSError(LaxIngestSchema):
    code: int
    domain: str


class Errno(LaxIngestSchema):
    number: int
    name: str | None


class MechanismMeta(LaxIngestSchema):
    posix_signal: PosixSignal | None = None
    match_exception: MachException | None = None
    ns_error: NSError | None = None
    errno: Errno | None = None
    relevant_address: str | None = None


class ExceptionMechanism(LaxIngestSchema):
    type: str
    description: str | None = None
    help_link: str | None = None
    handled: bool | None = None
    synthetic: bool | None = None
    is_exception_group: bool | None = None
    parent_id: int | None = None
    source: str | None = None
    meta: dict | None = None
    data: dict | None = None


class LockReason(LaxIngestSchema):
    type: int
    address: str | None = None
    package_name: str | None = None
    class_name: str | None = None
    thread_id: str | None = None


class StackTraceFrame(LaxIngestSchema):
    filename: str | None = None
    function: str | None = None
    raw_function: str | None = None
    function_id: str | None = None
    symbol: str | None = None
    module: str | None = None
    lineno: int | None = None
    colno: int | None = None
    abs_path: str | None = None
    context_line: str | None = None
    pre_context: list[str | None] | None = None
    post_context: list[str | None] | None = None
    source_link: str | None = None
    in_app: bool | None = None
    stack_start: bool | None = None
    lock: LockReason | None = None
    vars: dict[str, Union[str, dict, list]] | None = None
    instruction_addr: str | None = None
    addr_mode: str | None = None
    symbol_addr: str | None = None
    image_addr: str | None = None
    package: str | None = None
    platform: str | None = None

    def is_url(self, filename: str) -> bool:
        return filename.startswith(("file:", "http:", "https:", "applewebdata:"))

    @model_validator(mode="after")
    def normalize_files(self):
        if not self.abs_path and self.filename:
            self.abs_path = self.filename
        if self.filename and self.is_url(self.filename):
            self.filename = urlparse(self.filename).path
        return self

    @field_validator("pre_context", "post_context")
    @classmethod
    def replace_null(cls, context: list[str | None]) -> list[str | None] | None:
        if context:
            return [line if line else "" for line in context]
        return None


class StackTrace(LaxIngestSchema):
    frames: list[StackTraceFrame]
    registers: dict[str, str] | None = None


class EventException(LaxIngestSchema):
    type: str | None = None
    value: Annotated[str | None, WrapValidator(invalid_to_none)] = None
    module: str | None = None
    thread_id: str | None = None
    mechanism: Annotated[ExceptionMechanism | None, WrapValidator(invalid_to_none)] = (
        None
    )
    stacktrace: Annotated[StackTrace | None, WrapValidator(invalid_to_none)] = None
    raw_stacktrace: Annotated[StackTrace | None, WrapValidator(invalid_to_none)] = None


class ValueEventException(LaxIngestSchema):
    values: list[EventException]


class ExceptionEntryData(LaxIngestSchema):
    values: dict
    exc_omitted: None = None
    has_system_frames: bool


class ExceptionEntry(LaxIngestSchema):
    type: Literal["exception"]
    data: dict
