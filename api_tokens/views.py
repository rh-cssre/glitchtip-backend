from rest_framework import viewsets
from .models import APIToken
from .serializers import APITokenSerializer
from glitchtip.permissions import UserOnlyPermission


class APITokenViewSet(viewsets.ModelViewSet):
    queryset = APIToken.objects.all()
    serializer_class = APITokenSerializer
    permission_classes = [UserOnlyPermission]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        return self.queryset.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

