import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

from ninja import Field, Schema
from pydantic import (
    BeforeValidator,
    ConfigDict,
    GetJsonSchemaHandler,
    TypeAdapter,
    model_serializer,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import PydanticUndefined

from .base import LaxIngestSchema

logger = logging.getLogger(__name__)


class ExcludeNoneSchema(Schema):
    """
    Implements model_dump's exclude_none on the schema itself
    Useful for nested schemas where more granular control is needed
    Related https://github.com/pydantic/pydantic/discussions/5461
    """

    @model_serializer(mode="wrap")
    def ser_model(self, wrap: Callable) -> dict[str, Any]:
        if isinstance(self, Schema):
            return {
                model_field: getattr(self, model_field)
                for model_field in self.__class__.model_fields
                if getattr(self, model_field) is not None
            }
        return wrap(self)

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: Any,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Preserve field definitions in OpenAPI spec.

        Without this override the @model_serializer collapses the schema to
        a bare ``{type: object, additionalProperties: true}``.
        """
        json_schema = handler(core_schema)
        # If the serializer collapsed it to a bare object, rebuild from fields
        if "properties" not in json_schema and hasattr(cls, "model_fields"):
            field_props = {}
            required = []
            for field_name, field_info in cls.model_fields.items():
                try:
                    ta = TypeAdapter(field_info.annotation)
                    field_schema = ta.json_schema()
                    if (
                        field_info.default is not PydanticUndefined
                        and field_info.default is not None
                    ):
                        field_schema["default"] = field_info.default
                    elif field_info.default is PydanticUndefined:
                        required.append(field_name)
                    field_props[field_name] = field_schema
                except Exception:
                    logger.warning(
                        "Could not generate JSON schema for %s.%s",
                        cls.__name__,
                        field_name,
                    )
                    field_props[field_name] = {}
            json_schema["properties"] = field_props
            if required:
                json_schema["required"] = required
            json_schema.pop("additionalProperties", None)
        return json_schema


class DeviceContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["device"] = "device"
    name: str | None = None  # Inconsistency documented as required
    family: str | None = None  # Recommended but optional
    model: str | None = None  # Recommended but optional
    model_id: str | None = None
    arch: str | None = None
    battery_level: float | None = None
    orientation: str | None = None
    manufacturer: str | None = None
    brand: str | None = None
    screen_resolution: str | None = None
    screen_height_pixels: int | None = None
    screen_width_pixels: int | None = None
    screen_density: float | None = None
    screen_dpi: float | None = None
    online: bool | None = None
    charging: bool | None = None
    low_memory: bool | None = None
    simulator: bool | None = None
    memory_size: int | None = None
    free_memory: int | None = None
    usable_memory: int | None = None
    storage_size: int | None = None
    free_storage: int | None = None
    external_storage_size: int | None = None
    external_free_storage: int | None = None
    boot_time: str | None = None
    timezone: str | None = None  # Deprecated, use timezone of culture context
    language: str | None = None  # Deprecated, use locale of culture context
    processor_count: int | None = None
    cpu_description: str | None = None
    processor_frequency: float | None = None
    device_type: str | None = None
    battery_status: str | None = None
    device_unique_identifier: str | None = None
    supports_vibration: bool | None = None
    supports_accelerometer: bool | None = None
    supports_gyroscope: bool | None = None
    supports_audio: bool | None = None
    supports_location_service: bool | None = None

    model_config = ConfigDict(protected_namespaces=())


class OSContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["os"] = "os"
    name: str
    version: str | None = None
    build: str | None = None
    kernel_version: str | None = None
    rooted: bool | None = None
    theme: str | None = None
    raw_description: str | None = None  # Recommended but optional


class RuntimeContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["runtime"] = "runtime"
    name: str | None = None  # Recommended
    version: str | None = None
    raw_description: str | None = None


class AppContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["app"] = "app"
    app_start_time: str | None = None
    device_app_hash: str | None = None
    build_type: str | None = None
    app_identifier: str | None = None
    app_name: str | None = None
    app_version: str | None = None
    app_build: str | None = None
    app_memory: int | None = None
    in_foreground: bool | None = None


class BrowserContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["browser"] = "browser"
    name: str
    version: str | None = None


class GPUContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["gpu"] = "gpu"
    name: str
    version: str | None = None
    id: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    memory_size: int | None = None
    api_type: str | None = None
    multi_threaded_rendering: bool | None = None
    npot_support: str | None = None
    max_texture_size: int | None = None
    graphics_shader_level: str | None = None
    supports_draw_call_instancing: bool | None = None
    supports_ray_tracing: bool | None = None
    supports_compute_shaders: bool | None = None
    supports_geometry_shaders: bool | None = None


class StateContext(LaxIngestSchema):
    type: Literal["state"] = "state"
    state: dict


class CultureContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["culture"] = "culture"
    calendar: str | None = None
    display_name: str | None = None
    locale: str | None = None
    is_24_hour_format: bool | None = None
    timezone: str | None = None


class CloudResourceContext(LaxIngestSchema):
    type: Literal["cloud_resource"] = "cloud_resource"
    cloud: dict
    host: dict


class TraceContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["trace"] = "trace"
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    op: str | None = None
    status: str | None = None
    exclusive_time: float | None = None
    client_sample_rate: float | None = None
    tags: dict | list | None = None
    dynamic_sampling_context: dict | None = None
    origin: str | None = None


class ReplayContext(LaxIngestSchema):
    type: Literal["replay"] = "replay"
    replay_id: str


class ResponseContext(LaxIngestSchema):
    type: Literal["response"] = "response"
    status_code: int


ContextsUnion = Annotated[
    DeviceContext
    | OSContext
    | RuntimeContext
    | AppContext
    | BrowserContext
    | GPUContext
    | StateContext
    | CultureContext
    | CloudResourceContext
    | TraceContext
    | ReplayContext
    | ResponseContext,
    Field(discriminator="type"),
]


type_strings = [
    "device",
    "os",
    "runtime",
    "app",
    "browser",
    "gpu",
    "state",
    "culture",
    "cloud_resource",
    "trace",
    "replay",
    "response",
]


def default_types(v: Any) -> Any:
    if all(isinstance(value, dict) for value in v.values()):
        return {
            key: {
                **value,
                "type": key,
            }
            if key in type_strings and "type" not in value
            else value
            for key, value in v.items()
        }

    return v


# TODO warns Failed to get discriminator value for tagged union serialization with value
Contexts = Annotated[dict[str, ContextsUnion | Any], BeforeValidator(default_types)]
