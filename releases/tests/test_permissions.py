from io import StringIO
from django.urls import reverse
from django.core.files.uploadedfile import InMemoryUploadedFile
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class ReleaseAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.release = baker.make("releases.Release", organization=self.organization)
        self.release.projects.add(self.project)

        self.organization_list_url = reverse(
            "organization-releases-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        self.project_list_url = reverse(
            "project-releases-list",
            kwargs={"project_pk": self.organization.slug + "/" + self.project.slug},
        )
        self.organization_detail_url = reverse(
            "organization-releases-detail",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": self.release.version,
            },
        )
        self.project_detail_url = reverse(
            "project-releases-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "version": self.release.version,
            },
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.organization_list_url, 403)
        self.assertGetReqStatusCode(self.project_list_url, 403)
        self.auth_token.add_permission("project:read")
        self.assertGetReqStatusCode(self.organization_list_url, 200)
        self.assertGetReqStatusCode(self.project_list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.organization_detail_url, 403)
        self.assertGetReqStatusCode(self.project_detail_url, 403)
        self.auth_token.add_permission("project:read")
        self.assertGetReqStatusCode(self.organization_detail_url, 200)
        self.assertGetReqStatusCode(self.project_detail_url, 200)

    def test_assemble(self):
        url = self.organization_detail_url + "assemble/"
        data = {
            "checksum": "94bc085fe32db9b4b1b82236214d65eeeeeeeeee",
            "chunks": ["94bc085fe32db9b4b1b82236214d65eeeeeeeeee"],
        }
        self.assertPostReqStatusCode(url, data, 403)
        self.auth_token.add_permission("project:write")
        self.assertPostReqStatusCode(url, data, 200)

    def test_create(self):
        self.auth_token.add_permission("project:read")
        data = {"version": "new-version"}
        self.assertPostReqStatusCode(self.organization_list_url, data, 403)
        self.assertPostReqStatusCode(self.project_list_url, data, 403)
        self.auth_token.add_permission("project:releases")
        # Unsure if this should be supported
        # self.assertPostReqStatusCode(self.organization_list_url, data, 201)
        self.assertPostReqStatusCode(self.project_list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["project:read", "project:write"])
        self.assertDeleteReqStatusCode(self.project_detail_url, 403)

        self.auth_token.add_permission("project:releases")
        self.assertDeleteReqStatusCode(self.project_detail_url, 204)

    def test_user_destroy(self):
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.project_detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("project:read")
        data = {"version": "newer-version"}
        self.assertPutReqStatusCode(self.organization_detail_url, data, 403)

        self.auth_token.add_permission("project:releases")
        self.assertPutReqStatusCode(self.organization_detail_url, data, 200)


class ReleaseFileAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.release = baker.make(
            "releases.Release", organization=self.organization, projects=[self.project]
        )
        self.release_file = baker.make("releases.ReleaseFile", release=self.release)

        self.list_url = reverse(
            "files-list",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "release_version": self.release.version,
            },
        )
        self.detail_url = reverse(
            "files-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "release_version": self.release.version,
                "pk": self.release_file.pk,
            },
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("project:read")
        self.assertGetReqStatusCode(self.list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("project:read")
        self.assertGetReqStatusCode(self.detail_url, 200)

    def test_create(self):
        self.auth_token.add_permission("project:read")

        im_io = StringIO()
        file = InMemoryUploadedFile(
            im_io, None, "name.txt", "text/plain", len(im_io.getvalue()), None
        )
        data = {"name": "name", "file": file}

        self.assertPostReqStatusCode(self.list_url, data, 403)
        self.auth_token.add_permission("project:releases")
        self.assertPostReqStatusCode(self.list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["project:read", "project:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.auth_token.add_permission("project:releases")
        self.assertDeleteReqStatusCode(self.detail_url, 204)
