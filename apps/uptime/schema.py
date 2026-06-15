import socket
from typing import Annotated
from urllib.parse import urlparse

from annotated_types import Ge, Le
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.urls import reverse
from ninja import ModelSchema
from ninja.errors import ValidationError
from pydantic import ConfigDict, model_validator

from glitchtip.schema import CamelSchema

from .constants import HTTP_MONITOR_TYPES, MonitorType
from .models import Monitor, MonitorCheck, StatusPage
from .utils import is_ip_blocked


class MonitorCheckSchema(CamelSchema, ModelSchema):
    class Meta:
        model = MonitorCheck
        fields = ["is_up", "start_check", "reason"]


class MonitorCheckResponseTimeSchema(MonitorCheckSchema, ModelSchema):
    """Monitor check with response time. Used in Monitors detail api and monitor checks list"""

    class Meta(MonitorCheckSchema.Meta):
        fields = MonitorCheckSchema.Meta.fields + ["response_time"]


class MonitorIn(CamelSchema, ModelSchema):
    expected_body: str
    expected_status: int | None
    interval: Annotated[int, Ge(1), Le(86400)]
    timeout: Annotated[int, Ge(1), Le(60)] | None
    project: str | None = None

    @model_validator(mode="after")
    def validate(self):
        monitor_type = self.monitor_type
        if self.url == "" and monitor_type in HTTP_MONITOR_TYPES + (MonitorType.SSL,):
            raise ValidationError("URL is required for " + monitor_type)

        if monitor_type in HTTP_MONITOR_TYPES:
            try:
                URLValidator()(self.url)
            except DjangoValidationError as err:
                raise ValidationError("Invalid Url") from err

            if not settings.GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS:
                try:
                    parsed = urlparse(self.url)
                    hostname = parsed.hostname
                    if hostname:
                        # Check IP literal
                        try:
                            if is_ip_blocked(hostname):
                                raise ValidationError(
                                    "URLs targeting private/internal IPs are not allowed"
                                )
                        except ValueError:
                            pass
                        # Resolve and check
                        for info in socket.getaddrinfo(hostname, None):
                            if is_ip_blocked(info[4][0]):
                                raise ValidationError(
                                    "URLs targeting private/internal IPs are not allowed"
                                )
                except (socket.gaierror, OSError):
                    pass  # DNS resolution failure is not an SSRF concern

        if self.expected_status is None and monitor_type in [
            MonitorType.GET,
            MonitorType.POST,
        ]:
            raise ValidationError("Expected status is required for " + monitor_type)

        if monitor_type == MonitorType.PORT:
            url = self.url.replace("http://", "//", 1)
            if not url.startswith("//"):
                url = "//" + url
            parsed_url = urlparse(url)
            message = "Invalid Port URL, expected hostname and port"
            try:
                if not all([parsed_url.hostname, parsed_url.port]):
                    raise ValidationError(message)
            except ValueError as err:
                raise ValidationError(message) from err
            self.url = f"{parsed_url.hostname}:{parsed_url.port}"

        if (
            monitor_type == MonitorType.PORT
            and not settings.GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS
        ):
            hostname = self.url.split(":")[0]
            try:
                for info in socket.getaddrinfo(hostname, None):
                    if is_ip_blocked(info[4][0]):
                        raise ValidationError(
                            "URLs targeting private/internal IPs are not allowed"
                        )
            except (socket.gaierror, OSError):
                pass

        return self

    class Meta:
        model = Monitor
        fields = [
            "monitor_type",
            "name",
            "url",
            "interval",
        ]


class MonitorSchema(CamelSchema, ModelSchema):
    project_id: str | None
    environment_id: int | None
    is_up: bool | None
    last_change: str | None
    heartbeat_endpoint: str | None
    project_name: str | None = None
    env_name: str | None = None
    checks: list[MonitorCheckSchema]
    organization_id: int
    monitor_type: MonitorType

    class Meta:
        model = Monitor
        fields = [
            "id",
            "monitor_type",
            "endpoint_id",
            "created",
            "name",
            "url",
            "expected_status",
            "expected_body",
            "interval",
            "timeout",
        ]

    model_config = ConfigDict(coerce_numbers_to_str=True)

    @staticmethod
    def resolve_is_up(obj):
        return obj.latest_is_up

    @staticmethod
    def resolve_last_change(obj):
        if obj.last_change:
            return obj.last_change.isoformat().replace("+00:00", "Z")

    @staticmethod
    def resolve_heartbeat_endpoint(obj):
        if obj.endpoint_id:
            return settings.GLITCHTIP_URL.geturl() + reverse(
                "api:heartbeat_check",
                kwargs={
                    "organization_slug": obj.organization.slug,
                    "endpoint_id": obj.endpoint_id,
                },
            )

    @staticmethod
    def resolve_project_name(obj):
        if obj.project:
            return obj.project.name


class MonitorDetailSchema(MonitorSchema):
    checks: list[MonitorCheckResponseTimeSchema]


class StatusPageIn(CamelSchema, ModelSchema):
    is_public: bool = False

    class Meta:
        model = StatusPage
        fields = ["name"]


class StatusPageSchema(StatusPageIn, ModelSchema):
    monitors: list[MonitorSchema]

    class Meta(StatusPageIn.Meta):
        fields = ["name", "slug", "is_public"]
