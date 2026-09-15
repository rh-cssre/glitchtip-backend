import inspect
from dataclasses import dataclass
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Exists, OuterRef, QuerySet, Subquery
from django.http import Http404
from ninja.errors import HttpError
from ninja.pagination import paginate

from apps.organizations_ext.constants import ROLES, Scopes
from apps.organizations_ext.models import Organization, OrganizationUser
from glitchtip.base_models import OrgScopedModel

from .authentication import AuthHttpRequest


@dataclass(frozen=True)
class OrgScope:
    user_id: int
    org_slug: str
    required: frozenset[Scopes]
    roles: tuple[int, ...]  # role values holding any required scope

    @classmethod
    def from_request(cls, request, required, roles, kwargs) -> "OrgScope":
        """Token ceiling, then the scope the view will be gated by.

        Publishes itself on the request: ``OrgScopedHttpRequest`` promises
        views an ``org_scope``, and a gate that forgets to set it breaks that
        contract quietly.
        """
        if request.auth.auth_type == "token":
            effective = required & set(request.auth.data.get_scopes())
            if not effective:
                raise HttpError(
                    403,
                    f"Permission denied. This endpoint requires one of {sorted(required)}; "
                    "your token grants none of them.",
                )
            roles = tuple(i for i, r in enumerate(ROLES) if effective & r["scopes"])

        scope = cls(request.auth.user_id, kwargs["organization_slug"], required, roles)
        request.org_scope = scope  # set before the view runs
        return scope

    @property
    def org_id_expr(self) -> Subquery:
        # An UNCORRELATED scalar subquery, never a join to organization from
        # the data model.
        return Subquery(
            Organization.objects.filter(slug=self.org_slug, is_deleted=False).values(
                "id"
            )[:1]
        )

    def gate(self, org_id: Subquery) -> Exists:
        # No OuterRef. Deliberately UNCORRELATED, so Postgres evaluates
        # this once as an InitPlan instead of once per row. Adding an
        # OuterRef here looks more correct and silently makes it per-row.
        qs = OrganizationUser.objects.filter(
            user_id=self.user_id,
            organization_id=org_id,
        )
        if len(self.roles) < len(ROLES):
            # A no-op on routes every role qualifies for.
            qs = qs.filter(role__in=self.roles)
        return Exists(qs)

    def apply(self, qs: QuerySet) -> QuerySet:
        model = qs.model
        if not issubclass(model, OrgScopedModel):
            raise ImproperlyConfigured(
                f"{model._meta.label} is returned by a gated route but is not "
                "an OrgScopedModel, so the gate cannot isolate it. Declare "
                "org_field on it via OrgScopedModel."
            )
        org_id = self.org_id_expr
        return qs.filter(self.gate(org_id), **{model.org_field: org_id})

    async def explain_miss(self) -> HttpError | None:
        row = await (
            Organization.objects.filter(slug=self.org_slug, is_deleted=False)
            .annotate(
                membership_role=Subquery(
                    OrganizationUser.objects.filter(
                        organization_id=OuterRef("pk"), user_id=self.user_id
                    ).values("role")[:1]
                )
            )
            .values("membership_role")
            .afirst()
        )
        if row is None or row["membership_role"] is None:
            return HttpError(404, "Organization not found.")

        if row["membership_role"] in self.roles:
            return None

        role = ROLES[row["membership_role"]]
        if self.required & role["scopes"]:
            # The role holds a required scope but was still excluded, so the
            # token's scopes are the blocker.
            return HttpError(
                403,
                f"Permission denied. This endpoint requires one of "
                f"{sorted(self.required)}; your role ({role['name']}) grants "
                f"them but the API token you used does not.",
            )
        return HttpError(
            403,
            f"Permission denied. This endpoint requires one of "
            f"{sorted(self.required)}; your role in {self.org_slug} "
            f"({role['name']}) holds none of them.",
        )


def _resolve_roles(scopes: list[Scopes]) -> tuple[frozenset[Scopes], tuple[int, ...]]:
    """Import time: which role ints hold at least one required scope."""
    required = frozenset(scopes)
    # 'i' below is the same int used in OrganizationUserRole (0-member, 1-admin, etc.)
    # Also, this is not role >= X, but at least on scope derived from role are in scopes
    roles = tuple(i for i, r in enumerate(ROLES) if required & r["scopes"])
    if not roles:
        raise ImproperlyConfigured(f"no role holds any of {sorted(required)}")
    return required, roles


def _require_org_slug(view, parameters) -> None:
    """Both gates read ``organization_slug`` out of the path at request time."""
    if "organization_slug" not in parameters:
        raise ImproperlyConfigured(
            f"{view.__qualname__} is org-scoped but takes no "
            "`organization_slug` parameter -- the gate has no organization to "
            "scope it to"
        )


def org_scoped_list(scopes: list[Scopes], explain_empty: bool = False):
    """Folded gate for paginated views

    For pagination, we can capture the queryset, fold in authz, then paginate
    (evaluate).

    The model is only known once the view returns, so an un-scoped one is
    caught by ``apply`` on the first request rather than at import like
    ``org_scoped_qs``.

    ``explain_empty`` turns an empty page into the same 404/403 the
    non-paginated routes give, at the cost of a query on every empty page.
    Off by default: an empty page is an ordinary answer here, unlike the
    ``Http404`` that drives ``org_scoped_qs``.
    """
    required, roles = _resolve_roles(scopes)

    def decorator(view):
        _require_org_slug(view, inspect.signature(view).parameters)

        # The gate, with nothing about response shape in it: it folds into
        # whatever queryset the view hands back, and pagination happens after.
        @wraps(view)  # ninja reads the signature for path params
        async def gated(request, **kwargs):
            scope = OrgScope.from_request(request, required, roles, kwargs)
            qs = await view(request, **kwargs)
            if not isinstance(qs, QuerySet):
                raise TypeError(f"{view.__qualname__} must return a QuerySet")
            return scope.apply(qs)

        paginated = paginate(gated)
        if not explain_empty:
            return paginated

        # Outside ``paginate``, because the gate hands back an unevaluated
        # queryset -- there is nothing to look at until pagination has run it.
        @wraps(paginated)
        async def checked(request: OrgScopedHttpRequest, **kwargs):
            page = await paginated(request, **kwargs)

            if page:
                return page

            if error := await request.org_scope.explain_miss():
                raise error

            return page

        return checked

    return decorator


def org_scoped_qs(scopes: list[Scopes], model: type[OrgScopedModel]):
    """Folded gate for a view whose response is not one queryset.

    An aggregate, a bulk mutation, or a route that resolves a single object
    needs *rows* in the middle of its body, so by the time it returns there is
    nothing left for the gate to fold into. A decorator wraps -- it runs
    before the view and after it, never in the middle -- so the only way to
    gate these is to fold on the way IN rather than on the way out.

    ``model`` names the rows. The view is handed ``qs``: every row of that
    model in this organization that the caller's role reaches, and nothing
    else. Narrowing past that -- a path id, a slug, a filter -- is the view's
    own business and carries no isolation; roles are org-wide, and ``qs`` is
    already org-isolated and role-gated before the view sees it.
    """
    required, roles = _resolve_roles(scopes)
    if not (isinstance(model, type) and issubclass(model, OrgScopedModel)):
        raise ImproperlyConfigured(
            f"{model!r} is gated by @org_scoped_qs but is not an OrgScopedModel, "
            "so the gate cannot isolate it. Declare org_field on it via "
            "OrgScopedModel."
        )

    def decorator(view):
        signature = inspect.signature(view)
        _require_org_slug(view, signature.parameters)
        if "qs" not in signature.parameters:
            raise ImproperlyConfigured(
                f"{view.__qualname__} is @org_scoped_qs but takes no `qs` "
                "parameter -- there is nowhere to hand it the gated queryset"
            )

        @wraps(view)  # ninja reads the signature for path params
        async def gated(request, **kwargs):
            scope = OrgScope.from_request(request, required, roles, kwargs)
            qs = scope.apply(model._default_manager.all())
            try:
                return await view(request, qs=qs, **kwargs)
            except Http404:
                if error := await scope.explain_miss():
                    raise error from None
                raise

        # ninja builds the operation's parameters from the signature, and ``qs``
        # is ours to supply -- it is not a query param and must not show up in
        # the schema. ``@wraps`` leaves a ``__wrapped__`` that
        # ``inspect.signature`` follows straight back to the undecorated view,
        # so setting ``__signature__`` here is what stops that unwrap.
        gated.__signature__ = signature.replace(
            parameters=[
                param for name, param in signature.parameters.items() if name != "qs"
            ]
        )
        return gated

    return decorator


class OrgScopedHttpRequest(AuthHttpRequest):
    """A request under a folded gate, so ``org_scope`` is set.

    The folded gate never resolves an ``OrganizationUser`` row, so there is no
    role and no org id as an int -- only ``org_scope``, whose ``org_id_expr``
    is usable in a filter and nowhere else.
    """

    org_scope: OrgScope


def has_permission(permissions: list[str]):
    """
    Check scoped permissions. At this time only token authentication is checked.

    Example: @has_permission(["event:write", "event:admin"])

    The decorated function requires at least one of the specified permissions.
    """

    def decorator(f):
        @wraps(f)
        async def decorated_function(request: AuthHttpRequest, *args, **kwargs):
            if request.auth.auth_type == "token":
                scopes = request.auth.data.get_scopes()
                if not any(s in permissions for s in scopes):
                    raise HttpError(403, "Permission denied")
            return await f(request, *args, **kwargs)

        return decorated_function

    return decorator
