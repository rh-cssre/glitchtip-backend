from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api_tokens.schema import APITokenSchema
from users.serializers import UserSerializer


class APIRootView(APIView):
    """/api/0/ gives information about the server and current user"""

    def get(self, request, *args, **kwargs):
        user_data = None
        auth_data = None
        if request.user.is_authenticated:
            user_data = UserSerializer(instance=request.user).data
        if api_token := request.auth:
            auth_data = APITokenSchema(**api_token.__dict__).dict()
        return Response(
            {
                "version": "0",
                "user": user_data,
                "auth": auth_data,
            }
        )


async def health(request):
    return HttpResponse("ok", content_type="text/plain")
