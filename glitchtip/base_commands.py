from django.core.management.base import BaseCommand
from model_bakery import baker
from model_bakery.random_gen import gen_json, gen_slug

from apps.organizations_ext.models import Organization
from apps.projects.models import Project

baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("apps.organizations_ext.fields.OrganizationSlugField", gen_slug)
baker.generators.add("django.db.models.JSONField", gen_json)


class MakeSampleCommand(BaseCommand):
    organization = None
    project = None
    batch_size = 10000

    def add_org_project_arguments(self, parser):
        parser.add_argument("--org", type=str, help="Organization slug")
        parser.add_argument("--project", type=str, help="Project slug")

    def add_arguments(self, parser):
        parser.add_argument("--quantity", type=int, default=1000)
        self.add_org_project_arguments(parser)

    def handle(self, *args, **options):
        self.organization = self.get_organization(options.get("org"))
        self.project = self.get_project(options.get("project"))

    def get_organization(self, org: str):
        if org:
            return Organization.objects.get(slug=org)

        organization = Organization.objects.first()
        if not organization:
            organization = baker.make("organizations_ext.Organization")
        return organization

    def get_project(self, project: str):
        if project:
            return Project.objects.get(slug=project, organization=self.organization)
        project = Project.objects.filter(organization=self.organization).first()
        if not project:
            project = baker.make("projects.Project", organization=self.organization)
        return project

    def progress_tick(self):
        self.stdout.write(self.style.NOTICE("."), ending="")

    def success_message(self, message: str):
        self.stdout.write(self.style.SUCCESS(message))
