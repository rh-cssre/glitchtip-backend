from django.db.models import Count, Exists, OuterRef, Prefetch, QuerySet, Subquery

from apps.projects.models import Project
from apps.teams.models import Team

from .models import Organization, OrganizationUser


def get_organization_for_user(
    user_id: int, organization_slug: str
) -> QuerySet[Organization]:
    """Get organization queryset filtered by user membership and slug."""
    return Organization.objects.filter(users=user_id, slug=organization_slug)


def prefetch_organization_details(
    qs: QuerySet[Organization], user_id: int
) -> QuerySet[Organization]:
    return qs.prefetch_related(
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


def annotate_actor_role(qs: QuerySet, user_id: int, org_ref: str) -> QuerySet:
    """The caller's role in each row's organization, as ``actor_role``."""
    return qs.annotate(
        actor_role=Subquery(
            OrganizationUser.objects.filter(
                organization_id=OuterRef(org_ref), user_id=user_id
            ).values("role")[:1]
        )
    )


def select_organization_user_details(
    qs: QuerySet[OrganizationUser],
) -> QuerySet[OrganizationUser]:
    return qs.select_related("user", "organization__owner").prefetch_related(
        "user__socialaccount_set"
    )


def get_organizations_queryset(
    user_id, role_required=False, add_details=False, organization_slug=None
):
    qs = Organization.objects.filter(users=user_id, is_deleted=False)

    if organization_slug:
        qs = qs.filter(slug=organization_slug)

    if role_required or add_details:
        qs = qs.annotate(
            actor_role=Subquery(
                qs.filter(organization_users__user=user_id).values(
                    "organization_users__role"
                )[:1]
            )
        )
    if add_details:
        qs = prefetch_organization_details(qs, user_id)
    return qs
