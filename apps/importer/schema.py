from pydantic import HttpUrl

from glitchtip.schema import CamelSchema


class ImportIn(CamelSchema):
    url: HttpUrl
    auth_token: str
    organization_slug: str

    # def __init__(self, context, *args, **kwargs):
    #     if user := context["request"].user:
    #         self.fields[
    #             "organizationSlug"
    #         ].queryset = user.organizations_ext_organization.filter(
    #             organization_users__role__gte=OrganizationUserRole.ADMIN
    #         )
    #     return super().__init__(*args, **kwargs)
