from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.management import call_command
from django.http import Http404, HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from apps.teams.models import Team
from apps.uptime.models import Monitor
from apps.users.models import User


@csrf_exempt
def seed_data(request: HttpRequest):
    """
    Very destructive. Never enable on production.
    Generates data for e2e testing and deletes orgs it
    created previously as well as orgs created on
    behalf of e2e frontend. Always include `e2etestobj`
    in org name when creating orgs in e2e tests.
    """
    if settings.ENABLE_TEST_API is not True:
        raise Http404("Enable Test API is not enabled")

    user_email = "seeded-user@example.com"
    other_user_email = "second-seeded-user@example.com"
    user_password = "hunter22"  # nosec

    e2e_object_identifier = "e2etestobj"
    organization_name = e2e_object_identifier + "-seeded-org"

    team_slug = "seeded-team"
    project_name = "seeded-project"
    monitor_name = "seeded-monitor"

    User.objects.filter(email__in=[user_email, other_user_email]).delete()
    user = User.objects.create_user(email=user_email, password=user_password)
    other_user = User.objects.create_user(
        email=other_user_email, password=user_password
    )

    EmailAddress.objects.create(
        user=user, email=user_email, primary=True, verified=False
    )
    EmailAddress.objects.create(
        user=other_user, email=other_user_email, primary=True, verified=True
    )

    Organization.objects.filter(name__contains=e2e_object_identifier).delete()
    organization = Organization.objects.create(name=organization_name)
    orgUser = organization.add_user(user=user)

    team = Team.objects.create(slug=team_slug, organization=organization)
    project = Project.objects.create(name=project_name, organization=organization)

    Monitor.objects.filter(name__contains=e2e_object_identifier).delete()
    Monitor.objects.create(
        name=monitor_name,
        organization=organization,
        project=project,
        url="https://www.google.com",
        monitor_type="Ping",
        interval=60,
    )

    if request.GET.get("extras", None):
        project_name = "second-seeded-project"
        project2 = Project.objects.create(name=project_name, organization=organization)
        project_name = "third-seeded-project"
        project3 = Project.objects.create(
            name=project_name, organization=organization, platform="JavaScript"
        )
        team.projects.add(project)
        team.projects.add(project2)
        team.projects.add(project3)
        team.members.add(orgUser)

        if request.GET.get("seedIssues", None):
            call_command(
                "make_sample_issues",
                org=organization.slug,
                project=project3.slug,
                issue_quantity=55,
                events_quantity_per=1,
            )

    return HttpResponse()
