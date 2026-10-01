"""The org gate, tested through the alerts routes, its only consumer."""

from types import SimpleNamespace

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase
from django.test.utils import isolate_apps
from django.urls import reverse
from model_bakery import baker
from ninja import Router, Schema
from ninja.pagination import paginate
from ninja.testing import TestAsyncClient

from apps.alerts.models import AlertRecipient, ProjectAlert
from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization, OrganizationUser
from apps.projects.models import Project
from glitchtip.api.permissions import (
    NO_ORG_ID,
    OrgScope,
    _resolve_roles,
    org_scoped,
    org_scoped_list,
    resolve_org_id,
)
from glitchtip.base_models import ORG_SCOPED_MODELS, OrgScopedModel
from glitchtip.checks import (
    check_org_field,
    check_org_scoped_models,
    check_undeleted_through,
    registered_models,
)
from glitchtip.test_utils import generators  # noqa: F401


class OrgScopedModelDeclarationTestCase(SimpleTestCase):
    """``org_field`` is checked when the class is created, not per request."""

    def test_missing_or_empty_org_field_is_rejected(self):
        for label, attrs in [("missing", {}), ("empty", {"org_field": ""})]:
            with self.subTest(label), self.assertRaises(ImproperlyConfigured) as ctx:
                type(
                    "NoOrgField",
                    (OrgScopedModel, models.Model),
                    {
                        "__module__": __name__,
                        "Meta": type("Meta", (), {"app_label": "alerts"}),
                        **attrs,
                    },
                )
            self.assertIn("org_field", str(ctx.exception))

    def test_undeleted_through_that_is_not_a_tuple_of_paths_is_rejected(self):
        for value in ["project", ["project"], ("",)]:
            with (
                self.subTest(value=value),
                self.assertRaises(ImproperlyConfigured) as ctx,
            ):
                type(
                    "BadThrough",
                    (OrgScopedModel, models.Model),
                    {
                        "__module__": __name__,
                        "Meta": type("Meta", (), {"app_label": "alerts"}),
                        "org_field": "project__organization_id",
                        "undeleted_through": value,
                    },
                )
            self.assertIn("undeleted_through", str(ctx.exception))

    def test_org_field_that_joins_organization_is_rejected(self):
        # Both compile the same; _id keeps the path off organization's columns.
        with self.assertRaises(ImproperlyConfigured) as ctx:

            class JoinsOrganization(OrgScopedModel, models.Model):
                org_field = "project__organization"

                class Meta:
                    app_label = "alerts"

        self.assertIn("project__organization_id", str(ctx.exception))


@isolate_apps("apps.alerts")
class OrgScopedModelCheckTestCase(SimpleTestCase):
    """Resolves org_field paths once the app registry is ready."""

    def setUp(self):
        # The mixin registers every subclass; restore the registry.
        snapshot = ORG_SCOPED_MODELS[:]
        self.addCleanup(ORG_SCOPED_MODELS.__setitem__, slice(None), snapshot)

    def test_a_path_not_ending_on_the_organization_fk_is_rejected(self):
        class StopsAtProject(OrgScopedModel, models.Model):
            org_field = "project_id"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        # Runs, but joins organization and compares the wrong column.
        class ReachesOrganizationColumn(OrgScopedModel, models.Model):
            org_field = "project__organization__slug"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        for model in (StopsAtProject, ReachesOrganizationColumn):
            with self.subTest(model.org_field):
                error = check_org_field(model)
                self.assertEqual(error.id, "glitchtip.E002")
                self.assertIn(model.org_field, error.msg)

    def test_a_path_that_does_not_resolve_is_rejected(self):
        class Typo(OrgScopedModel, models.Model):
            org_field = "porject__organization_id"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        error = check_org_field(Typo)
        self.assertEqual(error.id, "glitchtip.E001")

    def test_a_path_that_crosses_a_multi_valued_relation_is_rejected(self):
        # A reverse relation duplicates rows and admits any related match.
        class Bucket(OrgScopedModel, models.Model):
            org_field = "items__project__organization_id"

            class Meta:
                app_label = "alerts"

        class Item(models.Model):
            bucket = models.ForeignKey(
                Bucket, related_name="items", on_delete=models.CASCADE
            )
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        error = check_org_field(Bucket)
        self.assertEqual(error.id, "glitchtip.E003")

    def test_an_undeleted_through_path_without_is_deleted_is_rejected(self):
        class NoSoftDelete(OrgScopedModel, models.Model):
            org_field = "project__organization_id"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)
            recipient = models.ForeignKey(AlertRecipient, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        # A relation whose model has no is_deleted, and a plain column.
        for path in ("recipient", "project__platform"):
            with self.subTest(path):
                NoSoftDelete.undeleted_through = (path,)
                error = check_undeleted_through(NoSoftDelete)
                self.assertEqual(error.id, "glitchtip.E004")
                self.assertIn(path, error.msg)

    def test_an_abstract_base_is_not_checked(self):
        # Registered, but it has no table to check.
        class AbstractBase(OrgScopedModel, models.Model):
            org_field = "project__organization_id"

            class Meta:
                abstract = True
                app_label = "alerts"

        self.assertIn(AbstractBase, ORG_SCOPED_MODELS)
        self.assertNotIn(AbstractBase, registered_models(None))
        self.assertEqual(check_org_scoped_models(None), [])


class OrgScopedModelRegistryTestCase(SimpleTestCase):
    """The registry as the running app sees it, not an isolated one."""

    def test_the_real_models_are_registered(self):
        # Declared models are validated even before a route uses them.
        labels = [model._meta.label for model in registered_models(None)]
        self.assertIn("alerts.ProjectAlert", labels)
        self.assertIn("projects.Project", labels)

    def test_the_check_narrows_to_the_apps_it_is_given(self):
        # ``manage.py check alerts`` should not walk the whole registry.
        labels = [
            model._meta.label
            for model in registered_models([apps.get_app_config("alerts")])
        ]
        self.assertIn("alerts.ProjectAlert", labels)
        self.assertNotIn("projects.Project", labels)


class OrgScopedDeclarationTestCase(SimpleTestCase):
    """``@org_scoped`` fails at import, not on the first request."""

    def test_model_that_is_not_org_scoped_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            org_scoped(["project:admin"], AlertRecipient)
        self.assertIn("OrgScopedModel", str(ctx.exception))

    def test_scope_no_role_holds_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            org_scoped(["project:nonexistent"], ProjectAlert)

    def test_view_without_a_qs_parameter_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:

            @org_scoped(["project:admin"], ProjectAlert)
            async def view(request, organization_slug: str):
                pass

        self.assertIn("qs", str(ctx.exception))

    def test_view_without_an_organization_slug_is_rejected(self):
        # Otherwise a KeyError 500 at request time.
        with self.assertRaises(ImproperlyConfigured) as ctx:

            @org_scoped(["project:admin"], ProjectAlert)
            async def view(request, qs, project_slug: str):
                pass

        self.assertIn("organization_slug", str(ctx.exception))

    def test_paginate_under_the_gate_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:

            @org_scoped(["project:read"], ProjectAlert)
            @paginate
            async def view(request, qs, organization_slug: str):
                return qs

        self.assertIn("@paginate above", str(ctx.exception))

    def test_the_real_routes_expose_the_right_parameters(self):
        """ninja must not expose qs; a leaked param is a 422 on every call."""
        from glitchtip.api.api import api

        paths = api.get_openapi_schema()["paths"]
        # The list route must be paginated; without it, it returns every row.
        for suffix, method, expected in [
            ("/{project_slug}/alerts/", "get", {"project_slug", "cursor", "limit"}),
            ("/alerts/{alert_id}/", "delete", {"alert_id"}),
        ]:
            with self.subTest(method):
                operations = [
                    item[method]
                    for path, item in paths.items()
                    if path.endswith(suffix) and method in item
                ]
                self.assertEqual(len(operations), 1)
                names = {p["name"] for p in operations[0].get("parameters", [])}
                self.assertNotIn("qs", names)
                self.assertLessEqual(expected, names)


class _Id(Schema):
    id: int


def _set_slug(obj, slug: str):
    """``AutoSlugField`` regenerates on insert, so a slug has to land after it."""
    type(obj).objects.filter(pk=obj.pk).update(slug=slug)
    obj.slug = slug
    return obj


class OrgScopedGateTestCase(TestCase):
    """Two orgs share a project slug; the user owns both, so the path must decide."""

    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.outsider = baker.make("users.user")
        cls.org = _set_slug(baker.make("organizations_ext.Organization"), "orga")
        cls.other_org = _set_slug(baker.make("organizations_ext.Organization"), "orgb")
        cls.org_user = cls.org.add_user(cls.user, OrganizationUserRole.OWNER)
        cls.other_org.add_user(cls.user, OrganizationUserRole.OWNER)
        cls.project = _set_slug(
            baker.make("projects.Project", organization=cls.org), "shared"
        )
        cls.other_project = _set_slug(
            baker.make("projects.Project", organization=cls.other_org), "shared"
        )
        cls.alert = baker.make("alerts.ProjectAlert", project=cls.project)
        cls.other_alert = baker.make("alerts.ProjectAlert", project=cls.other_project)
        cls.token = baker.make("api_tokens.APIToken", user=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def delete_url(self, org_slug="orga", project_slug="shared", alert_id=None):
        return reverse(
            "api:delete_project_alert",
            args=[
                org_slug,
                project_slug,
                self.alert.id if alert_id is None else alert_id,
            ],
        )

    def list_url(self, org_slug="orga", project_slug="shared"):
        return reverse("api:list_project_alerts", args=[org_slug, project_slug])

    def set_role(self, role: OrganizationUserRole):
        self.org_user.role = role
        self.org_user.save(update_fields=["role"])

    def bearer(self):
        self.client.logout()
        return {"authorization": f"Bearer {self.token.token}"}

    # Role gate

    def test_member_cannot_delete_and_is_told_their_role(self):
        self.set_role(OrganizationUserRole.MEMBER)
        for segment in ("orga", self.org.id):
            with self.subTest(segment=segment):
                res = self.client.delete(self.delete_url(org_slug=segment))
                self.assertEqual(res.status_code, 403)
                self.assertIn("Member", res.json()["detail"])
        self.assertTrue(ProjectAlert.objects.filter(id=self.alert.id).exists())

    # Existence hiding

    def test_an_org_you_cannot_see_is_indistinguishable_from_one_that_is_gone(self):
        self.client.force_login(self.outsider)
        hidden = self.client.delete(self.delete_url(org_slug="orga"))
        missing = self.client.delete(self.delete_url(org_slug="no-such-org"))
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.status_code, missing.status_code)
        self.assertEqual(hidden.json(), missing.json())
        self.assertTrue(ProjectAlert.objects.filter(id=self.alert.id).exists())

    def test_a_missing_row_is_a_plain_404_for_someone_entitled_to_it(self):
        # explain_miss stays silent when the gate wasn't the blocker.
        self.set_role(OrganizationUserRole.ADMIN)
        res = self.client.delete(self.delete_url(alert_id=self.alert.id + 1000))
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("Organization", res.json()["detail"])

    # Cross-org isolation on a shared slug

    def test_delete_cannot_reach_another_orgs_alert_through_a_shared_slug(self):
        self.set_role(OrganizationUserRole.ADMIN)
        for segment in ("orga", self.org.id):
            with self.subTest(segment=segment):
                res = self.client.delete(
                    self.delete_url(org_slug=segment, alert_id=self.other_alert.id)
                )
                self.assertEqual(res.status_code, 404)
        self.assertTrue(ProjectAlert.objects.filter(id=self.other_alert.id).exists())

    def test_list_is_scoped_to_the_org_in_the_path(self):
        cases = [
            (self.org.slug, self.alert),
            (self.org.id, self.alert),
            (self.other_org.slug, self.other_alert),
            (self.other_org.id, self.other_alert),
        ]
        for segment, alert in cases:
            with self.subTest(segment=segment):
                res = self.client.get(self.list_url(org_slug=segment))
                self.assertEqual(res.status_code, 200)
                self.assertEqual([row["id"] for row in res.json()], [alert.id])

    async def test_list_gates_the_queryset_the_view_returns(self):
        # A view that ignores qs must still only see the path org's live rows.
        await Project.objects.acreate(
            organization=self.org, name="gone", is_deleted=True
        )
        router = Router(
            auth=lambda request: SimpleNamespace(
                auth_type="session", user_id=self.user.id
            )
        )

        @router.get("/{organization_slug}/alerts/", response=list[_Id])
        @org_scoped_list(["project:read"], ProjectAlert)
        async def leaky_alerts(
            request, qs, response: HttpResponse, organization_slug: str
        ):
            return ProjectAlert.objects.all()

        @router.get("/{organization_slug}/projects/", response=list[_Id])
        @org_scoped_list(["project:read"], Project)
        async def leaky_projects(
            request, qs, response: HttpResponse, organization_slug: str
        ):
            return Project.objects.all()

        client = TestAsyncClient(router)
        for path, expected in [
            ("alerts", [self.alert.id]),
            ("projects", [self.project.id]),
        ]:
            with self.subTest(path):
                res = await client.get(f"/orga/{path}/")
                self.assertEqual(res.status_code, 200)
                self.assertEqual([row["id"] for row in res.json()], expected)

    # Path segments naming no reachable org

    def test_a_segment_naming_no_org_is_the_membership_404(self):
        # The user owns the deleted org, so only the deleted check stops them.
        deleted = _set_slug(baker.make("organizations_ext.Organization"), "gone")
        deleted.add_user(self.user, OrganizationUserRole.OWNER)
        project = _set_slug(
            baker.make("projects.Project", organization=deleted), "shared"
        )
        alert = baker.make("alerts.ProjectAlert", project=project)
        Organization.objects.filter(pk=deleted.pk).update(is_deleted=True)

        cases = [
            ("deleted org by id", deleted.id),
            ("deleted org by slug", "gone"),
            ("unused id", deleted.id + 1000),
            ("unknown slug", "no-such-org"),
            # Past int4: refused before any query.
            ("id past int4", str(2**31)),
            # The user owns self.org, so only the leading zero stops them.
            ("leading zero", f"0{self.org.id}"),
            # int() refuses strings past 4300 digits.
            ("huge id", "1" * 5000),
        ]
        for label, segment in cases:
            with self.subTest(label):
                res = self.client.delete(
                    self.delete_url(org_slug=segment, alert_id=alert.id)
                )
                self.assertEqual(res.status_code, 404)
                self.assertEqual(res.json()["detail"], "Organization not found.")
        self.assertTrue(ProjectAlert.objects.filter(id=alert.id).exists())

    async def test_non_ascii_digits_are_a_slug_not_an_id(self):
        # isdigit() accepts "²"; int() rejects it.
        self.assertEqual(await resolve_org_id("²"), NO_ORG_ID)

    # Token scopes

    def test_token_without_the_scope_is_refused(self):
        headers = self.bearer()
        res = self.client.delete(self.delete_url(), headers=headers)
        self.assertEqual(res.status_code, 403)
        self.assertIn("token", res.json()["detail"])

    def test_token_cannot_grant_more_than_the_users_role(self):
        self.set_role(OrganizationUserRole.MEMBER)
        self.token.add_permission("project:admin")
        res = self.client.delete(self.delete_url(), headers=self.bearer())
        self.assertEqual(res.status_code, 403)
        self.assertIn("Member", res.json()["detail"])
        self.assertTrue(ProjectAlert.objects.filter(id=self.alert.id).exists())

    async def test_a_role_that_qualifies_is_told_the_token_is_the_blocker(self):
        # Built by hand: no route has a qualifying role but a non-qualifying token.
        await OrganizationUser.objects.filter(pk=self.org_user.pk).aupdate(
            role=OrganizationUserRole.MEMBER
        )
        _, roles = _resolve_roles(["project:admin"])
        scope = OrgScope(
            self.user.id,
            self.org.id,
            frozenset({"project:read", "project:admin"}),
            roles,
        )
        error = await scope.explain_miss()
        self.assertEqual(error.status_code, 403)
        self.assertIn("the API token you used does not", error.message)

    # Cost

    # Sync: assertNumQueries doesn't see the async connection.
    def test_list_query_count(self):
        baker.make("alerts.ProjectAlert", project=self.project, _quantity=4)
        headers = self.bearer()
        self.token.add_permission("project:read")
        # Token, page, prefetch; the gate adds nothing by id, one lookup by slug.
        cases = [
            (self.org.id, 3, 200),
            ("orga", 4, 200),
            ("no-such-org", 3, 200),
        ]
        for segment, queries, status in cases:
            with self.subTest(segment=segment), self.assertNumQueries(queries):
                res = self.client.get(self.list_url(org_slug=segment), headers=headers)
            self.assertEqual(res.status_code, status)

    def test_an_empty_list_is_not_explained(self):
        # A non-member sees the same empty page an entitled caller with no
        # alerts would, whether or not the org exists.
        self.client.force_login(self.outsider)
        for segment in ("orga", "no-such-org"):
            with self.subTest(segment=segment):
                res = self.client.get(self.list_url(org_slug=segment))
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), [])
