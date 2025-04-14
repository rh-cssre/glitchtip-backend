from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .importer import GlitchTipImporter
from .schema import ImportIn

router = Router()


@router.post("import/")
@has_permission(["org:admin"])
async def importer(request: AuthHttpRequest, payload: ImportIn):
    organization = await aget_object_or_404(
        Organization,
        slug=payload.organization_slug,
        users=request.auth.user_id,
        organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    importer = GlitchTipImporter(
        str(payload.url), payload.auth_token, payload.organization_slug
    )
    await importer.check_auth()
    await importer.run(organization_id=organization.id)
