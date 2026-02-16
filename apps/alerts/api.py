from asgiref.sync import sync_to_async
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.pagination import paginate

from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .constants import RecipientType
from .models import AlertRecipient, ProjectAlert
from .schema import ProjectAlertIn, ProjectAlertSchema, TestAlertResultSchema
from .webhooks import send_test_notification

router = Router()

RECIPIENT_CONFIG_KEYS = {"bot_email", "api_key", "channel", "topic"}


def _prepare_recipient_data(recipient: dict) -> dict:
    """Move Zulip-specific flat fields into a config dict for storage."""
    config_data = {}
    cleaned = {}
    for key, value in recipient.items():
        if key in RECIPIENT_CONFIG_KEYS:
            config_data[key] = value
        else:
            cleaned[key] = value
    if config_data:
        cleaned["config"] = config_data
    return cleaned


def get_project_alert_queryset(user_id: int, organization_slug: str, project_slug: str):
    return ProjectAlert.objects.filter(
        project__organization__users=user_id,
        project__organization__slug=organization_slug,
        project__slug=project_slug,
    ).prefetch_related("alertrecipient_set")


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/",
    response=list[ProjectAlertSchema],
    by_alias=True,
)
@has_permission(["project:read", "project:write", "project:admin"])
@paginate
async def list_project_alerts(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
):
    return get_project_alert_queryset(
        request.auth.user_id, organization_slug, project_slug
    )


@router.post(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/",
    response={201: ProjectAlertSchema},
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def create_project_alert(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    payload: ProjectAlertIn,
):
    user_id = request.auth.user_id
    project = await aget_object_or_404(
        Project.objects.filter(
            Q(
                organization__users=user_id,
                organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
            | Q(teams__members__user=user_id)
        ).distinct(),
        organization__slug=organization_slug,
        slug=project_slug,
    )
    data = payload.dict(exclude_unset=True)
    recipients = data.pop("alert_recipients", [])
    project_alert = await project.projectalert_set.acreate(**data)
    await AlertRecipient.objects.abulk_create(
        [
            AlertRecipient(alert=project_alert, **_prepare_recipient_data(recipient))
            for recipient in recipients
        ]
    )
    return await get_project_alert_queryset(
        user_id, organization_slug, project_slug
    ).aget(id=project_alert.id)


@router.put(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}/",
    response=ProjectAlertSchema,
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def update_project_alert(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    alert_id: int,
    payload: ProjectAlertIn,
):
    user_id = request.auth.user_id
    alert = await aget_object_or_404(
        get_project_alert_queryset(user_id, organization_slug, project_slug),
        id=alert_id,
    )

    data = payload.dict(exclude_unset=True)
    alert_recipients = data.pop("alert_recipients", [])
    for attr, value in data.items():
        setattr(alert, attr, value)
    await alert.asave()

    # Create/Delete recipients as needed
    delete_recipient_ids = set(
        {id async for id in alert.alertrecipient_set.values_list("id", flat=True)}
    )
    for recipient in alert_recipients:
        prepared = _prepare_recipient_data(recipient)
        lookup = {
            "alert": alert,
            "recipient_type": prepared.pop("recipient_type"),
            "url": prepared.pop("url", ""),
        }
        new_recipient, created = await AlertRecipient.objects.aupdate_or_create(
            **lookup, defaults=prepared
        )
        if not created:
            delete_recipient_ids.discard(new_recipient.pk)
    if delete_recipient_ids:
        await alert.alertrecipient_set.filter(pk__in=delete_recipient_ids).adelete()

    return await get_project_alert_queryset(
        user_id, organization_slug, project_slug
    ).aget(id=alert.id)


@router.delete(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}/",
    response={204: None},
)
@has_permission(["project:admin"])
async def delete_project_alert(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, alert_id: int
):
    user_id = request.auth.user_id
    result, _ = (
        await get_project_alert_queryset(user_id, organization_slug, project_slug)
        .filter(id=alert_id)
        .filter(
            project__organization__users=user_id,
            project__organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
        )
        .adelete()
    )
    if result:
        return 204, None
    raise Http404


@router.post(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}/test/",
    response=list[TestAlertResultSchema],
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def test_project_alert(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    alert_id: int,
    recipient_id: int | None = None,
):
    alert = await aget_object_or_404(
        get_project_alert_queryset(
            request.auth.user_id, organization_slug, project_slug
        ).select_related("project__organization"),
        id=alert_id,
    )
    recipients = alert.alertrecipient_set.all()
    if recipient_id is not None:
        recipients = recipients.filter(id=recipient_id)
    results = []
    async for recipient in recipients:
        if recipient.recipient_type == RecipientType.EMAIL:
            results.append(
                {"recipient_type": "email", "status": "skipped", "message": None}
            )
            continue
        try:
            await sync_to_async(send_test_notification)(
                recipient.url,
                recipient.recipient_type,
                alert.project,
                tags_to_add=recipient.tags_to_add,
                config=recipient.config,
            )
            results.append(
                {
                    "recipient_type": recipient.recipient_type,
                    "status": "sent",
                    "message": None,
                }
            )
        except Exception as e:
            results.append(
                {
                    "recipient_type": recipient.recipient_type,
                    "status": "error",
                    "message": str(e),
                }
            )
    return results
