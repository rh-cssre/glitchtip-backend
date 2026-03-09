from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.api_tokens.models import APIToken
from apps.organizations_ext.models import Organization
from apps.projects.models import Project, ProjectKey
from apps.teams.models import Team
from apps.users.models import User

DEV_EMAIL = "test@example.com"
DEV_PASSWORD = "admin"
DEV_TOKEN = "d" * 64
ALL_SCOPES = [
    "project:read",
    "project:write",
    "project:admin",
    "project:releases",
    "team:read",
    "team:write",
    "team:admin",
    "event:read",
    "event:write",
    "event:admin",
    "org:read",
    "org:write",
    "org:admin",
    "member:read",
    "member:write",
    "member:admin",
]


class Command(BaseCommand):
    help = "Bootstrap a dev environment with a user, org, project, and API token"

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("bootstrap_dev requires DEBUG=True")

        user = self._get_or_create_user()
        self._ensure_verified_email(user)
        org = self._get_or_create_organization(user)
        project = self._get_or_create_project(org)
        team = self._get_or_create_team(org, user, project)
        token = self._get_or_create_token(user)
        dsn = ProjectKey.objects.filter(project=project).first()

        self.stdout.write(self.style.SUCCESS("\n=== Dev Environment Ready ==="))
        self.stdout.write(f"  User:     {DEV_EMAIL} / {DEV_PASSWORD}")
        self.stdout.write(f"  Org:      {org.slug}")
        self.stdout.write(f"  Project:  {project.slug}")
        self.stdout.write(f"  Team:     {team.slug}")
        self.stdout.write(f"  Token:    {token.token}")
        if dsn:
            self.stdout.write(f"  DSN:      {dsn.get_dsn()}")
        self.stdout.write(
            f"\n  curl -H 'Authorization: Bearer {token.token}' "
            f"http://localhost:8000/api/0/organizations/"
        )
        self.stdout.write("")

    def _get_or_create_user(self):
        try:
            return User.objects.get(email=DEV_EMAIL)
        except User.DoesNotExist:
            self.stdout.write(f"Creating user {DEV_EMAIL}")
            return User.objects.create_user(email=DEV_EMAIL, password=DEV_PASSWORD)

    def _ensure_verified_email(self, user):
        EmailAddress.objects.get_or_create(
            user=user,
            email=user.email,
            defaults={"primary": True, "verified": True},
        )

    def _get_or_create_organization(self, user):
        org, created = Organization.objects.get_or_create(
            slug="org", defaults={"name": "org"}
        )
        if created:
            self.stdout.write("Creating organization 'org'")
        if not org.users.filter(pk=user.pk).exists():
            org.add_user(user)
        return org

    def _get_or_create_project(self, org):
        project, created = Project.objects.get_or_create(
            slug="project", organization=org, defaults={"name": "project"}
        )
        if created:
            self.stdout.write("Creating project 'project'")
        return project

    def _get_or_create_team(self, org, user, project):
        team, created = Team.objects.get_or_create(
            slug="team", organization=org, defaults={}
        )
        if created:
            self.stdout.write("Creating team 'team'")
        org_user = org.organization_users.get(user=user)
        team.members.add(org_user)
        team.projects.add(project)
        return team

    def _get_or_create_token(self, user):
        try:
            return APIToken.objects.get(token=DEV_TOKEN)
        except APIToken.DoesNotExist:
            self.stdout.write("Creating API token")
            token = APIToken.objects.create(user=user, label="bootstrap_dev")
            # Override the auto-generated token with our known value
            APIToken.objects.filter(pk=token.pk).update(token=DEV_TOKEN)
            token.refresh_from_db()
            token.add_permissions(ALL_SCOPES)
            return token
