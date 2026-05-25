from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueEvent
from apps.issue_events.utils import get_entries
from apps.logs.api import LogEventRow
from apps.logs.constants import LogLevel
from apps.organizations_ext.models import Organization
from apps.performance.models import TransactionGroup
from apps.projects.models import Project


def serialize_organization(org: Organization) -> dict:
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "dateCreated": org.created.isoformat(),
        "isAcceptingEvents": org.is_accepting_events,
    }


def serialize_project(project: Project) -> dict:
    result = {
        "id": str(project.id),
        "name": project.name,
        "slug": project.slug,
        "platform": project.platform,
        "dateCreated": project.created.isoformat(),
    }
    if hasattr(project, "organization") and project.organization:
        result["organization"] = {
            "id": str(project.organization.id),
            "slug": project.organization.slug,
            "name": project.organization.name,
        }
    return result


def serialize_issue(issue: Issue) -> dict:
    result = {
        "id": str(issue.id),
        "title": issue.title,
        "culprit": issue.culprit or "",
        "level": issue.get_level_display(),
        "status": issue.get_status_display(),
        "type": issue.get_type_display(),
        "count": issue.count,
        "firstSeen": issue.first_seen.isoformat(),
        "lastSeen": issue.last_seen.isoformat(),
        "metadata": issue.metadata,
    }
    if hasattr(issue, "short_id_display"):
        result["shortId"] = issue.short_id_display
    if hasattr(issue, "project") and issue.project:
        result["project"] = {
            "id": str(issue.project.id),
            "name": issue.project.name,
            "slug": issue.project.slug,
        }
    if hasattr(issue, "num_comments"):
        result["numComments"] = issue.num_comments
    resolved_in_release = getattr(issue, "resolved_in_release", None)
    if issue.status == EventStatus.RESOLVED and resolved_in_release:
        result["statusDetails"] = {"inRelease": resolved_in_release.version}
    else:
        result["statusDetails"] = {}
    return result


def serialize_event(event: IssueEvent) -> dict:
    result = {
        "id": event.id.hex,
        "eventId": event.eventID,
        "issueId": str(event.issue_id),
        "title": event.title,
        "type": event.get_type_display(),
        "level": event.get_level_display(),
        "timestamp": event.timestamp.isoformat(),
        "transaction": event.transaction,
        "tags": [{"key": k, "value": v} for k, v in event.tags.items()],
    }
    if event.data:
        entries = get_entries(event.data)
        if entries:
            result["entries"] = [_serialize_entry(e) for e in entries]
        if contexts := event.data.get("contexts"):
            result["contexts"] = contexts
        if user := event.data.get("user"):
            result["user"] = user
        if sdk := event.data.get("sdk"):
            result["sdk"] = sdk
    return result


def _serialize_entry(entry) -> dict:
    """Convert entry schema objects to plain dicts."""
    if hasattr(entry, "model_dump"):
        return entry.model_dump(by_alias=True)
    if isinstance(entry, dict):
        return entry
    return {"type": str(type(entry).__name__), "data": str(entry)}


def serialize_alert(alert) -> dict:
    result = {
        "id": alert.id,
        "name": alert.name,
        "timespanMinutes": alert.timespan_minutes,
        "quantity": alert.quantity,
        "uptime": alert.uptime,
    }
    if hasattr(alert, "alertrecipient_set"):
        recipients = alert.alertrecipient_set.all()
        result["alertRecipients"] = [
            {
                "id": r.id,
                "recipientType": r.recipient_type,
                "url": r.url,
            }
            for r in recipients
        ]
    return result


def serialize_log_event(log: LogEventRow) -> dict:
    # Keep in sync with apps/logs/schema.py:LogEventSchema
    result = {
        "id": str(log.id),
        "timestamp": log.timestamp.isoformat(),
        "level": LogLevel(log.level).label,
        "body": log.body,
        "service": log.service,
        "environment": log.environment,
        "host": log.host,
        "projectId": log.project_id,
    }
    if log.trace_id:
        result["traceId"] = str(log.trace_id)
    if log.span_id is not None:
        value = log.span_id if log.span_id >= 0 else log.span_id + (1 << 64)
        result["spanId"] = f"{value:016x}"
    if log.severity_number is not None:
        result["severityNumber"] = log.severity_number
    if log.data:
        result["data"] = log.data
    return result


def serialize_transaction_group(tg: TransactionGroup) -> dict:
    return {
        "id": tg.id,
        "project": tg.project_id,
        "transaction": tg.transaction,
        "op": tg.op,
        "method": tg.method,
        "count": tg.count,
        "avgDuration": tg.avg_duration,
        "p50": tg.p50,
        "p95": tg.p95,
        "errorCount": tg.error_count,
        "errorRate": tg.error_rate,
        "throughput": tg.throughput,
        "firstSeen": tg.first_seen.isoformat(),
        "lastSeen": tg.last_seen.isoformat(),
    }


def serialize_n_plus_one_pattern(pattern: dict) -> dict:
    return {
        "transactionName": pattern["transaction_name"],
        "op": pattern["op"],
        "description": pattern["description"],
        "totalSpans": pattern["total_spans"],
        "transactionCount": pattern["transaction_count"],
        "spansPerTxn": pattern["spans_per_txn"],
        "avgDuration": pattern["avg_duration"],
        "totalTime": pattern["total_time"],
    }


def serialize_transaction_trend(trend: dict) -> dict:
    date = trend["date"]
    return {
        "date": date.isoformat() if hasattr(date, "isoformat") else date,
        "count": trend["count"],
        "transactionCount": trend["transaction_count"],
        "avgDuration": trend["avg_duration"],
        "totalTime": trend["total_time"],
    }


def serialize_span_group(span: dict) -> dict:
    return {
        "op": span["op"],
        "description": span["description"],
        "count": span["count"],
        "avgDuration": span["avg_duration"],
        "p95Duration": span["p95_duration"],
        "totalTime": span["total_time"],
    }


def serialize_monitor(monitor) -> dict:
    result = {
        "id": monitor.id,
        "name": monitor.name,
        "monitorType": monitor.monitor_type,
        "url": monitor.url,
        "interval": monitor.interval,
        "created": monitor.created.isoformat(),
        "expectedStatus": monitor.expected_status,
        "expectedBody": monitor.expected_body,
        "organizationId": monitor.organization_id,
    }
    if monitor.project_id:
        result["projectId"] = str(monitor.project_id)
    if hasattr(monitor, "latest_is_up"):
        result["isUp"] = monitor.latest_is_up
    if hasattr(monitor, "last_change") and monitor.last_change:
        result["lastChange"] = monitor.last_change.isoformat()
    return result
