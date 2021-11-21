from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import NotFound
from allauth.account.models import EmailAddress
from users.models import User
from organizations_ext.models import Organization
from teams.models import Team
from projects.models import Project
from glitchtip.uptime.models import Monitor


class SeedDataAPIView(APIView):
    """
    Delete existing data and seed data used in end to end testing
    Very destructive. Never enable on production.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        if settings.ENABLE_TEST_API is not True:
            raise NotFound("Enable Test API is not enabled")

        user_email = "cypresstest@example.com"
        other_user_email = "cypresstest-other@example.com"
        user_password = "hunter22"  # nosec
        organization_name = "Business Company, Inc."
        team_slug = "cypresstestteam"
        project_name = "NicheScrip"

        User.objects.filter(email=user_email).delete()
        user = User.objects.create_user(email=user_email, password=user_password)

        User.objects.filter(email=other_user_email).delete()
        other_user = User.objects.create_user(
            email=other_user_email, password=user_password
        )

        EmailAddress.objects.create(
            user=user, email=user_email, primary=True, verified=False
        )
        EmailAddress.objects.create(
            user=other_user, email=other_user_email, primary=True, verified=False
        )

        Organization.objects.filter(name=organization_name).delete()
        organization = Organization.objects.create(name=organization_name)
        orgUser = organization.add_user(user=user)

        Team.objects.filter(slug=team_slug).delete()
        team = Team.objects.create(slug=team_slug, organization=organization)

        Project.objects.filter(name=project_name).delete()
        project = Project.objects.create(name=project_name, organization=organization)

        Monitor.objects.filter(name="cytestmonitor").delete()
        Monitor.objects.create(
            name = "cytestmonitor",
            organization=organization,
            project=project,
            url="https://www.google.com",
            monitor_type="Ping",
            interval="00:01:00"
        )

        if (request.query_params.get("extras")):
            project_name = "SwitchGrip"
            project2 = Project.objects.create(name=project_name, organization=organization)
            project_name = "PitchFlip"
            project3 = Project.objects.create(name=project_name, organization=organization, platform="JavaScript")
            team.projects.add(project)
            team.projects.add(project2)
            team.projects.add(project3)
            team.members.add(orgUser)

        return Response()
