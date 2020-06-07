from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import NotFound
from users.models import User
from organizations_ext.models import Organization
from glitchtip import test_utils  # pylint: disable=unused-import


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
        user_password = "hunter22"
        organization_name = "coolbeans"

        User.objects.filter(email=user_email).delete()
        user = User.objects.create_user(email=user_email, password=user_password)

        Organization.objects.filter(name=organization_name).delete()
        organization = Organization.objects.create(name=organization_name)
        organization.add_user(user=user)

        return Response()
