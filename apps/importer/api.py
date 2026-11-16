from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from apps.organizations_ext.queryset_utils import annotate_actor_role
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .schema import ImportIn

router = Router()


@router.post("import/")
@has_permission(["org:admin"])
async def importer(request: AuthHttpRequest, payload: ImportIn):
    # Imported here so loading the API doesn't import aiohttp; see
    # glitchtip.ssrf_protection.
    from .importer import GlitchTipImporter

    organization = await aget_object_or_404(
        annotate_actor_role(
            Organization.objects.filter(slug=payload.organization_slug),
            request.auth.user_id,
            "pk",
        ),
        actor_role__gte=OrganizationUserRole.ADMIN,
    )
    importer = GlitchTipImporter(
        str(payload.url), payload.auth_token, payload.organization_slug
    )
    await importer.check_auth()
    await importer.run(
        organization_id=organization.id, max_role=organization.actor_role
    )
