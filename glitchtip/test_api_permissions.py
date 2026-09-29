"""The folded gate, exercised through the two routes that use it.

``apps/alerts`` is the only consumer so far -- ``list_project_alerts`` is
``@org_scoped_list`` and ``delete_project_alert`` is ``@org_scoped``
alone. What is under test is ``glitchtip.api.permissions``,
not alerts, so these move if the decorator grows a second consumer worth
testing it through.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import SimpleTestCase, TestCase
from django.test.utils import isolate_apps
from django.urls import reverse
from model_bakery import baker
from ninja.pagination import paginate

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
    registered_models,
)
from glitchtip.test_utils import generators  # noqa: F401


class OrgScopedModelDeclarationTestCase(SimpleTestCase):
    """``org_field`` is checked when the class is created, not per request."""

    def test_missing_org_field_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:

            class NoOrgField(OrgScopedModel, models.Model):
                class Meta:
                    app_label = "alerts"

        self.assertIn("org_field", str(ctx.exception))

    def test_empty_org_field_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):

            class BlankOrgField(OrgScopedModel, models.Model):
                org_field = ""

                class Meta:
                    app_label = "alerts"

    def test_org_field_that_joins_organization_is_rejected(self):
        # A spelling rule, enforced here because it is one: both spellings
        # compile to the same column comparison. ``_id`` states that the gate
        # stops at the foreign key, and keeps the path one segment away from
        # ``__organization__<column>``, which does join the table.
        with self.assertRaises(ImproperlyConfigured) as ctx:

            class JoinsOrganization(OrgScopedModel, models.Model):
                org_field = "project__organization"

                class Meta:
                    app_label = "alerts"

        self.assertIn("project__organization_id", str(ctx.exception))


@isolate_apps("apps.alerts")
class OrgScopedModelCheckTestCase(SimpleTestCase):
    """``manage.py check`` resolves what ``__init_subclass__`` cannot.

    The app registry is still filling when a model class is created, so the
    declaration can only be checked as a string there. The path it names is
    resolved later, by the system check.
    """

    def setUp(self):
        # The mixin appends every subclass it sees, and these tests create
        # models. Hand the registry back unchanged.
        snapshot = ORG_SCOPED_MODELS[:]
        self.addCleanup(ORG_SCOPED_MODELS.__setitem__, slice(None), snapshot)

    def test_the_registered_models_resolve(self):
        self.assertEqual(check_org_scoped_models(None), [])

    def test_a_path_that_stops_short_of_the_organization_is_rejected(self):
        class StopsAtProject(OrgScopedModel, models.Model):
            org_field = "project_id"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        error = check_org_field(StopsAtProject)
        self.assertEqual(error.id, "glitchtip.E002")
        self.assertIn("project_id", error.msg)

    def test_a_path_reaching_past_the_foreign_key_is_rejected(self):
        # Resolves, and filtering on it would even run -- it just joins
        # organization to compare the wrong column.
        class ReachesOrganizationColumn(OrgScopedModel, models.Model):
            org_field = "project__organization__slug"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        error = check_org_field(ReachesOrganizationColumn)
        self.assertEqual(error.id, "glitchtip.E002")

    def test_a_path_that_does_not_resolve_is_rejected(self):
        class Typo(OrgScopedModel, models.Model):
            org_field = "porject__organization_id"
            project = models.ForeignKey(Project, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        error = check_org_field(Typo)
        self.assertEqual(error.id, "glitchtip.E001")

    def test_a_path_that_crosses_a_multi_valued_relation_is_rejected(self):
        # Resolves, and ends on the organization foreign key -- but by joining
        # back down a reverse relation rather than walking up keys. The gate
        # would duplicate rows and admit a row whenever *any* related row is in
        # the organization, which is not isolation.
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

    def test_both_spellings_of_the_foreign_key_are_accepted(self):
        # ``__init_subclass__`` rejects the bare spelling, so the check must
        # not be the thing enforcing it -- two rules over one string, disagreeing
        # about which layer owns it, is how a valid model gets refused twice.
        class LocalKey(OrgScopedModel, models.Model):
            org_field = "organization_id"
            organization = models.ForeignKey(Organization, on_delete=models.CASCADE)

            class Meta:
                app_label = "alerts"

        self.assertIsNone(check_org_field(LocalKey))

    def test_an_abstract_base_is_not_checked(self):
        # It is still appended to the registry -- ``__init_subclass__`` cannot
        # tell yet -- but it has no table, and the path names a relation it
        # does not have, so resolving it would fail.
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
        # The reader that makes the registry worth keeping: a model declared
        # with the mixin is validated whether or not a route uses it yet.
        labels = [model._meta.label for model in registered_models(None)]
        self.assertIn("alerts.ProjectAlert", labels)
        self.assertIn("projects.Project", labels)

    def test_the_check_narrows_to_the_apps_it_is_given(self):
        # ``manage.py check alerts`` should not walk the whole registry.
        self.assertEqual(
            [
                model._meta.label
                for model in registered_models([apps.get_app_config("alerts")])
            ],
            ["alerts.ProjectAlert"],
        )


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
        # The gate reads it out of the path at request time, where a missing
        # one is a KeyError 500 rather than a refusal.
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

    def test_the_list_gate_keeps_the_import_time_guards(self):
        # It delegates to org_scoped, so these are the same guards, not copies.
        with self.assertRaises(ImproperlyConfigured):
            org_scoped_list(["project:admin"], AlertRecipient)

        with self.assertRaises(ImproperlyConfigured) as ctx:

            @org_scoped_list(["project:read"], ProjectAlert)
            async def view(request, organization_slug: str):
                return ProjectAlert.objects.all()

        self.assertIn("qs", str(ctx.exception))

    def test_the_list_gate_paginates(self):
        # The whole point of the wrapper: a list route cannot leave pagination
        # off, and leaving it off is otherwise a quiet 200 with every row.
        from glitchtip.api.api import api

        paths = api.get_openapi_schema()["paths"]
        operations = [
            item["get"]
            for path, item in paths.items()
            if path.endswith("/{project_slug}/alerts/") and "get" in item
        ]
        self.assertEqual(len(operations), 1)
        names = [p["name"] for p in operations[0].get("parameters", [])]
        self.assertIn("cursor", names)
        self.assertIn("limit", names)

    def test_qs_is_not_a_parameter_on_the_real_routes(self):
        """The signature edit is only worth anything if ninja honours it.

        An un-annotated param ninja can see becomes a required ``str`` query
        param, so a leak here is a 422 on every call, not a cosmetic one.
        Checked on the paginated route too, since ``@paginate`` wraps it again.
        """
        from glitchtip.api.api import api

        paths = api.get_openapi_schema()["paths"]
        for suffix, method, path_param in [
            ("/{project_slug}/alerts/", "get", "project_slug"),
            ("/alerts/{alert_id}/", "delete", "alert_id"),
        ]:
            with self.subTest(method):
                operations = [
                    item[method]
                    for path, item in paths.items()
                    if path.endswith(suffix) and method in item
                ]
                self.assertEqual(len(operations), 1)
                names = [p["name"] for p in operations[0].get("parameters", [])]
                self.assertNotIn("qs", names)
                self.assertIn(path_param, names)


def _set_slug(obj, slug: str):
    """``AutoSlugField`` regenerates on insert, so a slug has to land after it."""
    type(obj).objects.filter(pk=obj.pk).update(slug=slug)
    obj.slug = slug
    return obj


class OrgScopedGateTestCase(TestCase):
    """Two organizations sharing a project slug, one user in both.

    ``other_org`` is one the user *is* an owner of, so a row reached through
    the wrong slug proves the org in the path is being ignored rather than
    merely proving that membership filtering works.
    """

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

    def test_admin_can_delete(self):
        self.set_role(OrganizationUserRole.ADMIN)
        res = self.client.delete(self.delete_url())
        self.assertEqual(res.status_code, 204)
        self.assertFalse(ProjectAlert.objects.filter(id=self.alert.id).exists())

    # Existence hiding

    def test_non_member_gets_404_not_403(self):
        self.client.force_login(self.outsider)
        res = self.client.delete(self.delete_url())
        self.assertEqual(res.status_code, 404)
        self.assertTrue(ProjectAlert.objects.filter(id=self.alert.id).exists())

    def test_an_org_you_cannot_see_is_indistinguishable_from_one_that_is_gone(self):
        self.client.force_login(self.outsider)
        hidden = self.client.delete(self.delete_url(org_slug="orga"))
        missing = self.client.delete(self.delete_url(org_slug="no-such-org"))
        self.assertEqual(hidden.status_code, missing.status_code)
        self.assertEqual(hidden.json(), missing.json())

    def test_a_missing_row_is_a_plain_404_for_someone_entitled_to_it(self):
        # Entitled callers get the ordinary Http404, not the membership one:
        # explain_miss declines to speak when the gate was not the blocker.
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

    # Path segments naming no reachable org

    def test_a_segment_naming_no_org_is_the_membership_404(self):
        # The user owns the deleted org and it holds a matching alert, so only
        # the deleted check stands between them: by id, that check is the
        # gate's, since an id from the path skips the slug lookup.
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
            # Organization.id is int4; past it, the driver raises rather than
            # matching nothing.
            ("id past int4", str(2**31)),
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
        # The route's slug converter already refuses "²", so this pins the
        # guard for callers that don't come through a route: str.isdigit()
        # accepts it and int() then raises.
        self.assertEqual(await resolve_org_id("²"), NO_ORG_ID)

    # Token scopes

    def test_token_without_the_scope_is_refused(self):
        headers = self.bearer()
        res = self.client.delete(self.delete_url(), headers=headers)
        self.assertEqual(res.status_code, 403)
        self.assertIn("token", res.json()["detail"])

        self.token.add_permission("project:admin")
        res = self.client.delete(self.delete_url(), headers=headers)
        self.assertEqual(res.status_code, 204)

    def test_token_cannot_grant_more_than_the_users_role(self):
        self.set_role(OrganizationUserRole.MEMBER)
        self.token.add_permission("project:admin")
        res = self.client.delete(self.delete_url(), headers=self.bearer())
        self.assertEqual(res.status_code, 403)
        self.assertIn("Member", res.json()["detail"])
        self.assertTrue(ProjectAlert.objects.filter(id=self.alert.id).exists())

    async def test_a_role_that_qualifies_is_told_the_token_is_the_blocker(self):
        # No route yet requires two scopes through an explaining gate, so the
        # scope is built as from_request would for a token holding only
        # project:admin on a route accepting project:read or project:admin.
        # Member holds project:read, so the role qualifies and the token
        # doesn't.
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

    # Kept synchronous: assertNumQueries only observes Django's sync
    # connection, and the async test client runs the view on the async one.
    def test_list_query_count(self):
        baker.make("alerts.ProjectAlert", project=self.project, _quantity=4)
        headers = self.bearer()
        self.token.add_permission("project:read")
        # Token lookup, the page, the alertrecipient prefetch. By id, the gate
        # adds none of its own: the membership check is a subquery inside the
        # page query, which is the whole point of folding it in. A slug costs
        # one lookup ahead of the page.
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
