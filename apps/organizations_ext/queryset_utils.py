from django.db.models import Count, Exists, OuterRef, Prefetch, Subquery

from apps.projects.models import Project
from apps.teams.models import Team

from .models import Organization, OrganizationUser


def get_organizations_queryset(
    user_id, role_required=False, add_details=False, organization_slug=None
):
    qs = Organization.objects.filter(users=user_id)

    if organization_slug:
        qs = qs.filter(slug=organization_slug)

    if role_required:
        qs = qs.annotate(
            actor_role=Subquery(
                qs.filter(organization_users__user=user_id).values(
                    "organization_users__role"
                )[:1]
            )
        )
    if add_details:
        qs = qs.prefetch_related(
            Prefetch(
                "projects",
                queryset=Project.annotate_is_member(Project.objects, user_id),
            ),
            "projects__teams",
            Prefetch(
                "teams",
                queryset=Team.objects.annotate(
                    is_member=Exists(
                        OrganizationUser.objects.filter(
                            teams=OuterRef("pk"), user_id=user_id
                        )
                    ),
                    member_count=Count("members"),
                ),
            ),
            "teams__members",
        )
    return qs


def get_organization_users_queryset(
    user_id: int,
    organization_slug: str,
    team_slug: str | None = None,
    role_required=False,
    add_details=False,
):
    qs = (
        OrganizationUser.objects.filter(
            organization__users=user_id, organization__slug=organization_slug
        )
        .select_related("user", "organization__owner")
        .prefetch_related("user__socialaccount_set")
    )
    if team_slug:
        qs = qs.filter(teams__slug=team_slug)
    if role_required:
        qs = qs.annotate(
            actor_role=Subquery(
                qs.filter(organization__organization_users__user=user_id).values(
                    "organization__organization_users__role"
                )[:1]
            )
        )
    if add_details:
        qs = qs.prefetch_related("teams")
    return qs
