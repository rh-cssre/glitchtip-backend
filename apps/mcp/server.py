import json
import logging

from django.conf import settings
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

mcp = FastMCP(
    "glitchtip",
    stateless_http=True,
    auth_server_provider=GlitchTipOAuthProvider(),
    auth=AuthSettings(
        issuer_url=settings.GLITCHTIP_URL.geturl(),
        resource_server_url=settings.GLITCHTIP_URL.geturl(),
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
async def get_event(event_id: str) -> str:
    """Look up a specific event by its ID and return it with its parent issue.

    Accepts either format:
    - GlitchTip UUIDv7 (e.g. "019c4a0ac93a75f0a26ef22012159759")
    - Sentry SDK event_id UUID (e.g. "a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    Use this when a user provides an event ID from a URL, log, or alert.

    Args:
        event_id: Event UUID (either GlitchTip id or Sentry SDK event_id)
    """
    try:
        user_id = _check_scopes(["event:read", "event:write", "event:admin"])
        event = await data.get_event(user_id, event_id)
        if event is None:
            return _error("Event not found")
        result = serializers.serialize_event(event)
        result["issue"] = serializers.serialize_issue(event.issue)
        return json.dumps(result)
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
