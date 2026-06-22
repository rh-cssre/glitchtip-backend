"""Decode native OTLP log payloads into GlitchTip log item dicts.

This is the ingest path for vanilla OpenTelemetry exporters posting to
``/v1/logs`` (OTLP/HTTP), independent of any sentry SDK. We accept the two
OTLP/HTTP encodings the spec defines:

- ``application/x-protobuf`` — the default for OTel SDKs and the Collector.
- ``application/json`` — proto3 JSON mapping (camelCase keys, hex trace ids).

Both encodings are normalized into the same per-record dict shape that
``otel_log_to_log_item`` already consumes, so the record-level mapping
(severity, body, trace/span correlation, attribute flattening) lives in one
place. Resource-level attributes (``service.name``, ``deployment.environment``,
``host.name``, …) are hoisted onto each record before conversion.

https://opentelemetry.io/docs/specs/otlp/
https://opentelemetry.io/docs/specs/otel/logs/data-model/
"""

import orjson
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)

from .schema import LogItemSchema, otel_log_to_log_item

# Resource attributes that only describe the OTel SDK itself. They carry no
# end-user value and would otherwise be copied into every log row's JSONB.
_RESOURCE_ATTR_SKIP_PREFIXES = ("telemetry.sdk.", "telemetry.auto.")

# All-zero ids mean "no trace/span context"; treat them as absent rather than
# storing a meaningless 0 / zero-UUID.
_ZERO_TRACE_ID = b"\x00" * 16
_ZERO_SPAN_ID = b"\x00" * 8


def _proto_anyvalue_to_dict(value) -> dict | None:
    """Convert a protobuf ``AnyValue`` to the snake_case dict form that
    ``_extract_otel_value`` unwraps. Non-scalar values are JSON-stringified."""
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind in ("string_value", "int_value", "double_value", "bool_value"):
        return {kind: getattr(value, kind)}
    if kind == "bytes_value":
        return {"string_value": value.bytes_value.hex()}
    # array_value / kvlist_value — rare for logs; preserve as a JSON string.
    return {"string_value": orjson.dumps(MessageToDict(value)).decode()}


def _keep_resource_attr(key: str) -> bool:
    return not key.startswith(_RESOURCE_ATTR_SKIP_PREFIXES)


def _proto_record_to_otel_dict(record) -> dict:
    """Build an OTLP-log-record dict (snake_case, hex ids) from a protobuf
    ``LogRecord``, matching what ``otel_log_to_log_item`` expects."""
    body = _proto_anyvalue_to_dict(record.body)
    rec: dict = {
        "time_unix_nano": record.time_unix_nano or record.observed_time_unix_nano,
        # severity_number is the enum's integer value; 0 == UNSPECIFIED.
        "severity_number": record.severity_number or None,
        "severity_text": record.severity_text or None,
        # An unset body must map to "" (not the literal "None" str(None) would
        # produce downstream).
        "body": body if body is not None else "",
        "attributes": [
            {"key": attr.key, "value": _proto_anyvalue_to_dict(attr.value)}
            for attr in record.attributes
        ],
    }
    # trace_id (16 bytes) / span_id (8 bytes) are hex-encoded per the OTLP/JSON
    # convention; empty or all-zero bytes mean the record was not inside a span.
    if record.trace_id and record.trace_id != _ZERO_TRACE_ID:
        rec["trace_id"] = record.trace_id.hex()
    if record.span_id and record.span_id != _ZERO_SPAN_ID:
        rec["span_id"] = record.span_id.hex()
    return rec


def _to_log_item_dicts(records: list[dict], resource_attrs: list[dict]) -> list[dict]:
    """Convert OTLP-record dicts to validated LogItemSchema dicts, hoisting
    resource attributes onto each record (record-level keys win)."""
    items: list[dict] = []
    for rec in records:
        # Resource attrs first so a same-key record attribute overrides them.
        rec["attributes"] = resource_attrs + (rec.get("attributes") or [])
        converted = otel_log_to_log_item(rec)
        items.append(LogItemSchema(**converted).dict())
    return items


def _decode_protobuf(body: bytes) -> list[dict]:
    request = ExportLogsServiceRequest()
    request.ParseFromString(body)

    items: list[dict] = []
    for resource_logs in request.resource_logs:
        resource_attrs = [
            {"key": attr.key, "value": _proto_anyvalue_to_dict(attr.value)}
            for attr in resource_logs.resource.attributes
            if _keep_resource_attr(attr.key)
        ]
        records = [
            _proto_record_to_otel_dict(record)
            for scope_logs in resource_logs.scope_logs
            for record in scope_logs.log_records
        ]
        items.extend(_to_log_item_dicts(records, resource_attrs))
    return items


def _decode_json(body: bytes) -> list[dict]:
    """Decode OTLP/JSON. Field names may be camelCase (proto3 JSON) or
    snake_case; both are tolerated here and downstream."""
    data = orjson.loads(body)
    resource_logs = data.get("resourceLogs") or data.get("resource_logs") or []

    items: list[dict] = []
    for rl in resource_logs:
        resource = rl.get("resource") or {}
        resource_attrs = [
            attr
            for attr in (resource.get("attributes") or [])
            if isinstance(attr, dict) and _keep_resource_attr(attr.get("key", ""))
        ]
        scope_logs = rl.get("scopeLogs") or rl.get("scope_logs") or []
        records = [
            dict(record)
            for sl in scope_logs
            for record in (sl.get("logRecords") or sl.get("log_records") or [])
        ]
        items.extend(_to_log_item_dicts(records, resource_attrs))
    return items


def decode_otlp_logs(body: bytes, content_type: str) -> list[dict]:
    """Decode an OTLP logs export request body into LogItemSchema dicts ready
    for the ``ingest_logs`` task. Multiplexes on Content-Type per the OTLP/HTTP
    spec; defaults to protobuf, which is what OTel SDKs and the Collector send."""
    if "json" in content_type:
        return _decode_json(body)
    return _decode_protobuf(body)
