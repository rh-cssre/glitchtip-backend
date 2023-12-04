from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from api_tokens.serializers import APITokenAuthScopesSerializer
from users.serializers import UserSerializer


class APIRootView(APIView):
    """/api/0/ gives information about the server and current user"""

    def get(self, request, *args, **kwargs):
        user_data = None
        auth_data = None
        if request.user.is_authenticated:
            user_data = UserSerializer(instance=request.user).data
        if request.auth:
            auth_data = APITokenAuthScopesSerializer(instance=request.auth).data
        return Response(
            {
                "version": "0",
                "user": user_data,
                "auth": auth_data,
            }
        )


def health(request):
    return HttpResponse("ok", content_type="text/plain")
