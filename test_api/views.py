from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import NotFound
from users.models import User


class SeedDataAPIView(APIView):
    """
    Delete existing data and seed data used in end to end testing
    Very destructive. Never enable on production.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        if settings.ENABLE_TEST_API is not True:
            raise NotFound("Enable Test API is not enabled")

        try:
            test_user = User.objects.get(email="cypresstest@example.com")
        except User.DoesNotExist:
            test_user = None

        if test_user:
            test_user.delete()

        User.objects.create_user(email="cypresstest@example.com", password="hunter22")
        return Response()
