import inspect
from dataclasses import dataclass
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Exists, QuerySet, Subquery
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

    @property
    def org_id_expr(self) -> Subquery:
        # An UNCORRELATED scalar subquery, never a join to organization from
        # the data model -- that measured ~14x slower.
        return Subquery(
            Organization.objects.filter(slug=self.org_slug, is_deleted=False).values(
                "id"
            )[:1]
        )

    @property
    def gate(self) -> Exists:
        # No OuterRef. Deliberately UNCORRELATED, so Postgres evaluates
        # this once as an InitPlan instead of once per row. Adding an
        # OuterRef here looks more correct and silently makes it per-row.
        qs = OrganizationUser.objects.filter(
            user_id=self.user_id,
            organization_id=self.org_id_expr,
        )
        if len(self.roles) < len(ROLES):
            # A no-op on event:* routes, where every role qualifies.
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
        return qs.filter(self.gate, **{model.org_field: self.org_id_expr})


def _resolve_roles(scopes: list[Scopes]) -> tuple[frozenset[Scopes], tuple[int, ...]]:
    """Import time: which role ints hold at least one required scope."""
    required = frozenset(scopes)
    roles = tuple(i for i, r in enumerate(ROLES) if required & r["scopes"])
    if not roles:
        raise ImproperlyConfigured(f"no role holds any of {sorted(required)}")
    return required, roles


async def _enter_scope(request, required, roles, kwargs) -> OrgScope:
    """Token ceiling, then the scope the view will be gated by."""
    if request.auth.auth_type == "token":
        effective = required & set(request.auth.data.get_scopes())
        if not effective:
            raise HttpError(
                403,
                f"Permission denied. This endpoint requires one of {sorted(required)}; "
                "your token grants none of them.",
            )
        roles = tuple(i for i, r in enumerate(ROLES) if effective & r["scopes"])

    scope = OrgScope(request.auth.user_id, kwargs["organization_slug"], required, roles)
    request.org_scope = scope  # set before the view runs
    return scope


def _org_gate(required: frozenset[Scopes], roles: tuple[int, ...], view):
    """The gate, with nothing about response shape in it.

    Checks the token, builds the ``OrgScope``, runs the view, and folds the
    gate into whatever queryset the view hands back. What happens to that
    queryset afterwards -- paginated, or evaluated whole -- is the caller's
    business, and is the ONLY thing the wrappers below differ in.
    """

    @wraps(view)  # ninja reads the signature for path params
    async def gated(request, **kwargs):
        scope = await _enter_scope(request, required, roles, kwargs)
        qs = await view(request, **kwargs)
        if not isinstance(qs, QuerySet):
            raise TypeError(f"{view.__qualname__} must return a QuerySet")
        return scope.apply(qs)

    return gated


def org_scoped_list(scopes: list[Scopes]):
    """Folded gate for paginated views

    only ~14 endpoints are paginated. For pagination, we can capture the
    queryset, fold in authz, then paginate (evaluate).
    """
    required, roles = _resolve_roles(scopes)

    def decorator(view):
        return paginate(_org_gate(required, roles, view))

    return decorator


def org_scoped_qs(scopes: list[Scopes], queryset):
    """Folded gate for a view whose response is not one queryset.

    An aggregate, a bulk mutation, or a route that resolves a single object
    needs *rows* in the middle of its body, so by the time it returns there is
    nothing left for ``_org_gate`` to fold into. A decorator wraps -- it runs
    before the view and after it, never in the middle -- so the only way to
    gate these is to fold on the way IN rather than on the way out.

    ``queryset`` is an async ``(request, **kwargs) -> QuerySet`` called once the
    scope exists, with ninja's already-parsed parameters, so it can narrow by
    ``filters`` or a path id. Whatever it returns is gated before the view sees
    it and handed over as ``qs``.
    """
    required, roles = _resolve_roles(scopes)

    def decorator(view):
        signature = inspect.signature(view)
        if "qs" not in signature.parameters:
            raise ImproperlyConfigured(
                f"{view.__qualname__} is @org_scoped_qs but takes no `qs` "
                "parameter -- there is nowhere to hand it the gated queryset"
            )

        @wraps(view)  # ninja reads the signature for path params
        async def gated(request, **kwargs):
            scope = await _enter_scope(request, required, roles, kwargs)
            return await view(
                request, qs=scope.apply(await queryset(request, **kwargs)), **kwargs
            )

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
