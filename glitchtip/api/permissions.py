import inspect
from dataclasses import dataclass
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Exists, QuerySet
from django.http import Http404
from ninja.errors import HttpError
from ninja.pagination import paginate

from apps.organizations_ext.constants import ROLES, Scopes
from apps.organizations_ext.models import Organization, OrganizationUser
from glitchtip.base_models import OrgScopedModel, SoftDeleteModel

from .authentication import AuthHttpRequest

# Organization.id is an int4 AutoField, this is the upper bound.
MAX_ORG_ID = 2**31 - 1
NO_ORG_ID = 0

ORG_NOT_FOUND = "Organization not found."
# The path parameter every gated route names its organization by.
ORG_PARAM = "organization_id_or_slug"


async def resolve_org_id(segment: str) -> int:
    """The org id a path segment names: all digits is an id, anything else a slug.

    An id costs no query; a slug costs one lookup. A miss is ``NO_ORG_ID``, so
    the gate treats it like any org the caller isn't in.
    """
    # isdigit() alone accepts non-ASCII digits such as "²", which int() rejects.
    if segment.isascii() and segment.isdigit():
        # One spelling per id: a leading zero (or "0" itself) names no org.
        if segment[0] == "0" or len(segment) > len(str(MAX_ORG_ID)):
            return NO_ORG_ID
        org_id = int(segment)
        return org_id if org_id <= MAX_ORG_ID else NO_ORG_ID
    org_id = await (
        Organization.objects.filter(slug=segment, is_deleted=False)
        .values_list("id", flat=True)
        .afirst()
    )
    return NO_ORG_ID if org_id is None else org_id


@dataclass(frozen=True)
class OrgScope:
    user_id: int
    org_id: int
    required: frozenset[Scopes]
    roles: tuple[int, ...]  # role values holding any required scope

    @classmethod
    async def from_request(cls, request, required, roles, kwargs) -> "OrgScope":
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

        org_id = await resolve_org_id(kwargs[ORG_PARAM])
        scope = cls(request.auth.user_id, org_id, required, roles)
        request.org_scope = scope  # set before the view runs
        return scope

    def gate(self) -> Exists:
        # No OuterRef. Deliberately UNCORRELATED, so Postgres evaluates
        # this once as an InitPlan instead of once per row. Adding an
        # OuterRef here looks more correct and silently makes it per-row.
        qs = OrganizationUser.objects.filter(
            user_id=self.user_id,
            organization_id=self.org_id,
            # An id from the path is unchecked, so a deleted org is caught here.
            organization__is_deleted=False,
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
        live = {f"{path}__is_deleted": False for path in model.undeleted_through}
        if issubclass(model, SoftDeleteModel):
            # Here, not in a manager: a list view's returned queryset is
            # gated too, and it may not have come from undeleted_objects.
            live["is_deleted"] = False
        return qs.filter(self.gate(), **{model.org_field: self.org_id}, **live)

    async def explain_miss(self) -> HttpError | None:
        membership_role = await (
            OrganizationUser.objects.filter(
                user_id=self.user_id,
                organization_id=self.org_id,
                organization__is_deleted=False,
            )
            .values_list("role", flat=True)
            .afirst()
        )
        if membership_role is None:
            return HttpError(404, ORG_NOT_FOUND)

        if membership_role in self.roles:
            return None

        role = ROLES[membership_role]
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
            f"{sorted(self.required)}; your role in this organization "
            f"({role['name']}) holds none of them.",
        )


def _resolve_roles(scopes: list[Scopes]) -> tuple[frozenset[Scopes], tuple[int, ...]]:
    """Import time: which role ints hold at least one required scope."""
    required = frozenset(scopes)
    # 'i' below is the same int used in OrganizationUserRole (0-member, 1-admin, etc.)
    # Also, this is not role >= X: a role qualifies if at least one of its
    # scopes is in scopes.
    roles = tuple(i for i, r in enumerate(ROLES) if required & r["scopes"])
    if not roles:
        raise ImproperlyConfigured(f"no role holds any of {sorted(required)}")
    return required, roles


def org_scoped(scopes: list[Scopes], model: type[OrgScopedModel]):
    """Hand the view ``qs``: the undeleted rows of ``model`` the caller may reach.

    Only ``qs`` is gated; ``Model.objects`` in the view is not. (List routes
    use ``org_scoped_list``, which gates the queryset the view returns.)

    ``organization_id_or_slug`` carries the org's id or its slug (see
    ``resolve_org_id``).
    """
    required, roles = _resolve_roles(scopes)
    _require_scoped_model(model, "org_scoped")

    def decorator(view):
        if getattr(view, "_ninja_is_paginated", False):
            # The other way up, the gate wraps a page rather than the view's
            # queryset. Only this order is exercised, so only it is allowed.
            raise ImproperlyConfigured(
                f"{view.__qualname__} has @paginate under @org_scoped; move "
                "@paginate above it"
            )
        signature = inspect.signature(view)
        _require_org_param(view, signature.parameters, "org_scoped")
        if "qs" not in signature.parameters:
            raise ImproperlyConfigured(
                f"{view.__qualname__} is @org_scoped but takes no `qs` "
                "parameter -- there is nowhere to hand it the gated queryset"
            )

        @wraps(view)  # ninja reads the signature for path params
        async def gated(request, **kwargs):
            scope = await OrgScope.from_request(request, required, roles, kwargs)
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
                param for key, param in signature.parameters.items() if key != "qs"
            ]
        )
        return gated

    return decorator


def org_scoped_list(scopes: list[Scopes], model: type[OrgScopedModel]):
    """Gate the queryset a list view returns, once, then paginate it.

    ``paginate`` evaluates whatever the view returns, so that is what gets
    gated. The view takes no ``qs``: gating one as well would put the gate in
    the SQL twice. ``model`` is only checked at import.
    """
    required, roles = _resolve_roles(scopes)
    _require_scoped_model(model, "org_scoped_list")

    def decorator(view):
        parameters = inspect.signature(view).parameters
        _require_org_param(view, parameters, "org_scoped_list")
        if "qs" in parameters:
            raise ImproperlyConfigured(
                f"{view.__qualname__} is @org_scoped_list but takes `qs`; a "
                "list view builds its own queryset and the gate applies to it"
            )

        @wraps(view)  # ninja reads the signature for path params
        async def gated(request, **kwargs):
            scope = await OrgScope.from_request(request, required, roles, kwargs)
            try:
                result = await view(request, **kwargs)
            except Http404:
                if error := await scope.explain_miss():
                    raise error from None
                raise
            if not isinstance(result, QuerySet):
                raise TypeError(f"{view.__qualname__} must return a QuerySet")
            return scope.apply(result)

        return paginate(gated)

    return decorator


def _require_scoped_model(model, decorator: str) -> None:
    if not (isinstance(model, type) and issubclass(model, OrgScopedModel)):
        raise ImproperlyConfigured(
            f"{model!r} is gated by @{decorator} but is not an OrgScopedModel, "
            "so the gate cannot isolate it. Declare org_field on it via "
            "OrgScopedModel."
        )


def _require_org_param(view, parameters, decorator: str) -> None:
    if ORG_PARAM not in parameters:
        raise ImproperlyConfigured(
            f"{view.__qualname__} is @{decorator} but takes no "
            f"`{ORG_PARAM}` parameter -- the gate has no organization "
            "to scope it to"
        )


class OrgScopedHttpRequest(AuthHttpRequest):
    """A request under a folded gate, so ``org_scope`` is set.

    The folded gate doesn't resolve an ``OrganizationUser`` row, so there is
    no role here. ``org_scope.org_id`` is the path's org id, but nothing has
    checked it yet: membership is only proven inside ``qs``, so filter with
    ``org_scope.apply``, never with the bare id.
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
