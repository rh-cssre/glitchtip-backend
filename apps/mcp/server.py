import json
import logging
from datetime import datetime, timezone

from django.conf import settings as django_settings
from django.core.exceptions import FieldError
from django.http import Http404
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from apps.oauth.provider import DEFAULT_SCOPES, VALID_SCOPES, GlitchTipOAuthProvider

from . import data, serializers

logger = logging.getLogger(__name__)

_base_url = django_settings.GLITCHTIP_URL.geturl().rstrip("/")

mcp = FastMCP(
    "glitchtip",
    stateless_http=True,
    auth_server_provider=GlitchTipOAuthProvider(),
    auth=AuthSettings(
        issuer_url=_base_url,
        resource_server_url=f"{_base_url}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=VALID_SCOPES,
            default_scopes=DEFAULT_SCOPES,
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string, returning None if not provided."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(f"Invalid datetime format: {value!r}. Use ISO 8601.")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _get_user_id() -> int:
    """Extract user_id from the MCP auth context.

    Raises ValueError if the request is not authenticated.
    """
    access_token = get_access_token()
    if not access_token:
        raise ValueError("Not authenticated")
    return int(access_token.client_id)


def _check_scopes(required_scopes: list[str]) -> int:
    """Get user_id from auth context and verify the token has required scopes.

    Raises ValueError if not authenticated or lacking scopes.
    """
    access_token = get_access_token()
    if not access_token:
        raise ValueError("Not authenticated")
    if not any(s in required_scopes for s in access_token.scopes):
        raise ValueError("Token lacks required scope")
    return int(access_token.client_id)


@mcp.tool()
async def list_organizations() -> str:
    """List all organizations the authenticated user has access to."""
    try:
        user_id = _check_scopes(["org:read", "org:write", "org:admin"])
        orgs = await data.get_organizations(user_id)
        return json.dumps([serializers.serialize_organization(o) for o in orgs])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_projects(organization_slug: str) -> str:
    """List all projects in an organization."""
    try:
        user_id = _check_scopes(["project:read", "project:write", "project:admin"])
        projects = await data.get_projects(user_id, organization_slug)
        return json.dumps([serializers.serialize_project(p) for p in projects])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_issues(
    organization_slug: str,
    project_slug: str | None = None,
    query: str | None = None,
    sort: str | None = None,
    limit: int = 25,
) -> str:
    """List issues for an organization, optionally filtered by project.

    By default returns all issues (including resolved). Use query="is:unresolved"
    to show only active issues, which is usually what you want for investigating
    current problems.

    Args:
        organization_slug: Organization slug
        project_slug: Optional project slug to filter by
        query: Search query. Examples: "is:unresolved", "is:resolved",
            "level:error", or free text search. Combine with spaces.
        sort: Sort field: "-last_seen" (default), "-count", "-priority",
            "-first_seen"
        limit: Max issues to return (default 25, max 100)
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        if limit < 1:
            return _error("limit must be at least 1")
        issues = await data.get_issues(
            user_id,
            organization_slug,
            project_slug=project_slug,
            query=query,
            sort=sort,
            limit=limit,
        )
        return json.dumps([serializers.serialize_issue(i) for i in issues])
    except ValueError as e:
        return _error(str(e))
    except Http404:
        return _error(f"Organization '{organization_slug}' not found")
    except FieldError:
        return _error(f"Invalid sort field: {sort}")


@mcp.tool()
async def get_issue(issue_id: int) -> str:
    """Get details for a single issue by ID."""
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        issue = await data.get_issue(user_id, issue_id)
        if issue is None:
            return _error("Issue not found")
        return json.dumps(serializers.serialize_issue(issue))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def get_latest_event(issue_id: int) -> str:
    """Get the latest event for an issue."""
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        event = await data.get_latest_event(user_id, issue_id)
        if event is None:
            return _error("No events found for this issue")
        return json.dumps(serializers.serialize_event(event))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def get_event(
    event_id: str, organization_slug: str | None = None
) -> str:
    """Look up a specific event by its ID and return it with its parent issue.

    Accepts either format:
    - GlitchTip UUIDv7 (e.g. "019c4a0ac93a75f0a26ef22012159759")
    - Sentry SDK event_id UUID (e.g. "a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    Use this when a user provides an event ID from a URL, log, or alert.

    Args:
        event_id: Event UUID (either GlitchTip id or Sentry SDK event_id)
        organization_slug: Optional org slug for faster lookup (recommended)
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        event = await data.get_event(user_id, event_id, organization_slug)
        if event is None:
            return _error("Event not found")
        result = serializers.serialize_event(event)
        result["issue"] = serializers.serialize_issue(event.issue)
        return json.dumps(result)
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def update_issue(
    issue_id: int,
    status: str,
    in_next_release: bool = False,
    in_release: str | None = None,
) -> str:
    """Update an issue's status. Use this after investigating an issue to
    resolve, unresolve, or ignore it.

    Args:
        issue_id: Issue ID to update
        status: New status — one of "resolved", "unresolved", "ignored"
        in_next_release: If true and resolving, mark resolved in the latest
            release for the issue's project
        in_release: If provided and resolving, mark resolved in this specific
            release version string
    """
    try:
        user_id = _check_scopes(["event:write", "event:admin"])
        valid_statuses = ("resolved", "unresolved", "ignored")
        if status not in valid_statuses:
            return _error(
                f"Invalid status: {status!r}. Must be one of: {', '.join(valid_statuses)}"
            )
        issue = await data.update_issue(
            user_id,
            issue_id,
            status,
            in_next_release=in_next_release,
            in_release=in_release,
        )
        if issue is None:
            return _error("Issue not found")
        return json.dumps(serializers.serialize_issue(issue))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_alerts(
    organization_slug: str,
    project_slug: str | None = None,
) -> str:
    """List alert rules for an organization, optionally filtered by project.

    Args:
        organization_slug: Organization slug
        project_slug: Optional project slug to filter by
    """
    try:
        user_id = _check_scopes(["project:read", "project:write", "project:admin"])
        alerts = await data.get_alerts(
            user_id, organization_slug, project_slug=project_slug
        )
        return json.dumps([serializers.serialize_alert(a) for a in alerts])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_monitors(organization_slug: str) -> str:
    """List uptime monitors for an organization."""
    try:
        user_id = _check_scopes(["project:read", "project:write", "project:admin"])
        monitors = await data.get_monitors(user_id, organization_slug)
        return json.dumps([serializers.serialize_monitor(m) for m in monitors])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_transaction_groups(
    organization_slug: str,
    project_id: int | None = None,
    query: str | None = None,
    sort: str = "-avg_duration",
    limit: int = 25,
) -> str:
    """List transaction groups for an organization.

    Shows endpoints/operations with their performance stats (avg duration,
    p50, p95, count, error count). Useful for identifying slow endpoints.

    Args:
        organization_slug: Organization slug
        project_id: Optional project ID to filter by
        query: Search text in transaction name (case-insensitive)
        sort: Sort field: "-avg_duration" (default), "avg_duration",
            "-count", "count", "-created", "created"
        limit: Max results to return (default 25, max 100)
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        project_ids = [project_id] if project_id else None
        groups = await data.get_transaction_groups(
            user_id,
            organization_slug,
            project_ids=project_ids,
            query=query,
            sort=sort,
            limit=limit,
        )
        return json.dumps([serializers.serialize_transaction_group(g) for g in groups])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def get_transaction_group(organization_slug: str, group_id: int) -> str:
    """Get details for a single transaction group by ID.

    Args:
        organization_slug: Organization slug
        group_id: Transaction group ID
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        group = await data.get_transaction_group(user_id, organization_slug, group_id)
        if group is None:
            return _error("Transaction group not found")
        return json.dumps(serializers.serialize_transaction_group(group))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_transaction_spans(
    organization_slug: str,
    group_id: int,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Get span breakdown for a specific transaction group.

    Shows child spans grouped by (op, description) with timing stats.
    Useful for understanding where time is spent within a transaction.
    Requires DuckDB cold storage to be enabled.

    Args:
        organization_slug: Organization slug
        group_id: Transaction group ID
        start: Start datetime (ISO 8601). Defaults to 7 days ago.
        end: End datetime (ISO 8601). Defaults to now.
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        spans = await data.get_transaction_spans(
            user_id,
            organization_slug,
            group_id,
            start_dt=_parse_datetime(start),
            end_dt=_parse_datetime(end),
        )
        return json.dumps([serializers.serialize_span_group(s) for s in spans])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_span_groups(
    organization_slug: str,
    project_id: int | None = None,
    op: str | None = None,
    sort: str = "-total_time",
    limit: int = 50,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Query span groups across the organization (slow queries, etc).

    Groups spans by (op, description) across all transactions. Use op="db"
    to find slow database queries, or leave empty for all span types.
    Requires DuckDB cold storage to be enabled.

    Args:
        organization_slug: Organization slug
        project_id: Optional project ID to filter by
        op: Optional op prefix filter (e.g. "db" for database spans)
        sort: Sort field: "-total_time" (default), "-avg_duration", "-count"
        limit: Max results to return (default 50, max 100)
        start: Start datetime (ISO 8601). Defaults to 7 days ago.
        end: End datetime (ISO 8601). Defaults to now.
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        project_ids = [project_id] if project_id else None
        spans = await data.get_span_groups(
            user_id,
            organization_slug,
            project_ids=project_ids,
            op_filter=op,
            sort=sort,
            limit=limit,
            start_dt=_parse_datetime(start),
            end_dt=_parse_datetime(end),
        )
        return json.dumps([serializers.serialize_span_group(s) for s in spans])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def detect_n_plus_one(
    organization_slug: str,
    project_id: int | None = None,
    op: str | None = "db",
    threshold: float = 5.0,
    limit: int = 50,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Detect N+1 query patterns across transactions.

    Finds span groups that repeat many times per transaction (e.g. a DB
    query executed 50 times in a single request). High spans_per_txn
    values strongly suggest N+1 patterns.

    Requires DuckDB cold storage to be enabled.

    Args:
        organization_slug: Organization slug
        project_id: Optional project ID to filter by
        op: Op prefix filter (default "db" for database spans)
        threshold: Minimum spans-per-transaction to report (default 5.0)
        limit: Max results to return (default 50, max 100)
        start: Start datetime (ISO 8601). Defaults to 7 days ago.
        end: End datetime (ISO 8601). Defaults to now.
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        project_ids = [project_id] if project_id else None
        patterns = await data.get_n_plus_one_patterns(
            user_id,
            organization_slug,
            project_ids=project_ids,
            op_filter=op,
            threshold=threshold,
            limit=limit,
            start_dt=_parse_datetime(start),
            end_dt=_parse_datetime(end),
        )
        return json.dumps(
            [serializers.serialize_n_plus_one_pattern(p) for p in patterns]
        )
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def get_transaction_trend(
    organization_slug: str,
    group_id: int,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Get daily performance trend for a specific transaction group.

    Returns daily stats (request count, avg duration, total time) for the
    last 7 days. Useful for spotting performance regressions or improvements
    over time.

    Requires DuckDB cold storage to be enabled.

    Args:
        organization_slug: Organization slug
        group_id: Transaction group ID
        start: Start datetime (ISO 8601). Defaults to 7 days ago.
        end: End datetime (ISO 8601). Defaults to now.
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        trend = await data.get_transaction_trend(
            user_id,
            organization_slug,
            group_id,
            start_dt=_parse_datetime(start),
            end_dt=_parse_datetime(end),
        )
        return json.dumps([serializers.serialize_transaction_trend(t) for t in trend])
    except ValueError as e:
        return _error(str(e))


if django_settings.GLITCHTIP_ENABLE_LOGS:

    @mcp.tool()
    async def list_logs(
        organization_slug: str,
        project_id: int | None = None,
        level: str | None = None,
        service: str | None = None,
        environment: str | None = None,
        query: str | None = None,
        trace_id: str | None = None,
        limit: int = 50,
    ) -> str:
        """Search log events for an organization (last 7 days).

        Returns logs from most recent to oldest. Useful for investigating
        application behavior, debugging errors, and correlating with traces.

        Args:
            organization_slug: Organization slug
            project_id: Optional project ID to filter by
            level: Log level filter (trace, debug, info, warn, error, fatal)
            service: Filter by service name (exact match)
            environment: Filter by environment (exact match)
            query: Search text in log body (case-insensitive)
            trace_id: Filter by trace ID for correlation
            limit: Max logs to return (default 50, max 100)
        """
        try:
            user_id = _check_scopes(["event:read", "event:write", "event:admin"])
            logs = await data.get_logs(
                user_id,
                organization_slug,
                project_id=project_id,
                level=level,
                service=service,
                environment=environment,
                query=query,
                trace_id=trace_id,
                limit=limit,
            )
            return json.dumps([serializers.serialize_log_event(log) for log in logs])
        except ValueError as e:
            return _error(str(e))

    @mcp.tool()
    async def get_log(organization_slug: str, log_id: str) -> str:
        """Get a single log event by its ID.

        Args:
            organization_slug: Organization slug
            log_id: Log event UUID
        """
        try:
            user_id = _check_scopes(["event:read", "event:write", "event:admin"])
            log = await data.get_log(user_id, organization_slug, log_id)
            if log is None:
                return _error("Log event not found")
            return json.dumps(serializers.serialize_log_event(log))
        except ValueError as e:
            return _error(str(e))
