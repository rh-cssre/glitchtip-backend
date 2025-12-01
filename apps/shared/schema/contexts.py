from typing import Annotated, Any, Callable, Literal, Optional, TypedDict

from ninja import Field, Schema
from pydantic import BeforeValidator, ConfigDict, model_serializer

from .base import LaxIngestSchema


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
                for model_field in self.model_fields
                if getattr(self, model_field) is not None
            }
        return wrap(self)


class DeviceContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["device"] = "device"
    name: Optional[str] = None  # Inconsistency documented as required
    family: Optional[str] = None  # Recommended but optional
    model: Optional[str] = None  # Recommended but optional
    model_id: Optional[str] = None
    arch: Optional[str] = None
    battery_level: Optional[float] = None
    orientation: Optional[str] = None
    manufacturer: Optional[str] = None
    brand: Optional[str] = None
    screen_resolution: Optional[str] = None
    screen_height_pixels: Optional[int] = None
    screen_width_pixels: Optional[int] = None
    screen_density: Optional[float] = None
    screen_dpi: Optional[float] = None
    online: Optional[bool] = None
    charging: Optional[bool] = None
    low_memory: Optional[bool] = None
    simulator: Optional[bool] = None
    memory_size: Optional[int] = None
    free_memory: Optional[int] = None
    usable_memory: Optional[int] = None
    storage_size: Optional[int] = None
    free_storage: Optional[int] = None
    external_storage_size: Optional[int] = None
    external_free_storage: Optional[int] = None
    boot_time: Optional[str] = None
    timezone: Optional[str] = None  # Deprecated, use timezone of culture context
    language: Optional[str] = None  # Deprecated, use locale of culture context
    processor_count: Optional[int] = None
    cpu_description: Optional[str] = None
    processor_frequency: Optional[float] = None
    device_type: Optional[str] = None
    battery_status: Optional[str] = None
    device_unique_identifier: Optional[str] = None
    supports_vibration: Optional[bool] = None
    supports_accelerometer: Optional[bool] = None
    supports_gyroscope: Optional[bool] = None
    supports_audio: Optional[bool] = None
    supports_location_service: Optional[bool] = None

    model_config = ConfigDict(protected_namespaces=())


class OSContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["os"] = "os"
    name: str
    version: Optional[str] = None
    build: Optional[str] = None
    kernel_version: Optional[str] = None
    rooted: Optional[bool] = None
    theme: Optional[str] = None
    raw_description: Optional[str] = None  # Recommended but optional


class RuntimeContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["runtime"] = "runtime"
    name: str | None = None  # Recommended
    version: str | None = None
    raw_description: str | None = None


class AppContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["app"] = "app"
    app_start_time: Optional[str] = None
    device_app_hash: Optional[str] = None
    build_type: Optional[str] = None
    app_identifier: Optional[str] = None
    app_name: Optional[str] = None
    app_version: Optional[str] = None
    app_build: Optional[str] = None
    app_memory: Optional[int] = None
    in_foreground: Optional[bool] = None


class BrowserContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["browser"] = "browser"
    name: str
    version: Optional[str] = None


class GPUContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["gpu"] = "gpu"
    name: str
    version: Optional[str] = None
    id: Optional[str] = None
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    memory_size: Optional[int] = None
    api_type: Optional[str] = None
    multi_threaded_rendering: Optional[bool] = None
    npot_support: Optional[str] = None
    max_texture_size: Optional[int] = None
    graphics_shader_level: Optional[str] = None
    supports_draw_call_instancing: Optional[bool] = None
    supports_ray_tracing: Optional[bool] = None
    supports_compute_shaders: Optional[bool] = None
    supports_geometry_shaders: Optional[bool] = None


class StateContext(LaxIngestSchema):
    type: Literal["state"] = "state"
    state: dict


class CultureContext(LaxIngestSchema, ExcludeNoneSchema):
    type: Literal["culture"] = "culture"
    calendar: Optional[str] = None
    display_name: Optional[str] = None
    locale: Optional[str] = None
    is_24_hour_format: Optional[bool] = None
    timezone: Optional[str] = None


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


class ContextsDict(TypedDict):
    device: DeviceContext
    os: OSContext
    runtime: RuntimeContext
    app: AppContext
    browser: BrowserContext
    gpu: GPUContext
    state: StateContext
    culture: CultureContext
    cloud_resource: CloudResourceContext
    trace: TraceContext
    replay: ReplayContext
    response: ResponseContext


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
