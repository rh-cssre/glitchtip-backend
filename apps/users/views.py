from rest_framework import exceptions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.projects.models import UserProjectAlert

from .models import User
from .serializers import (
    CurrentUserSerializer,
    UserSerializer,
)


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(
                organizations_ext_organization__slug=organization_slug,
                organizations_ext_organization__users=self.request.user,
            )
        else:
            queryset = queryset.filter(id=self.request.user.id)
        return queryset

    def get_object(self):
        if self.kwargs.get("pk") == "me":
            return self.request.user
        return super().get_object()

    def get_serializer_class(self):
        if self.kwargs.get("pk") == "me":
            return CurrentUserSerializer
        return super().get_serializer_class()

    @action(
        detail=True, methods=["get", "post", "put"], url_path="notifications/alerts"
    )
    def alerts(self, request, pk=None):
        """
        Returns dictionary of project_id: status. Now project_id status means it's "default"

        To update, submit `{project_id: status}` where status is -1 (default), 0, or 1
        """
        user = self.get_object()
        alerts = user.userprojectalert_set.all()

        if request.method == "GET":
            data = {}
            for alert in alerts:
                data[alert.project_id] = alert.status
            return Response(data)

        data = request.data
        try:
            items = [x for x in data.items()]
        except AttributeError as err:
            raise exceptions.ValidationError(
                "Invalid alert format, expected dictionary"
            ) from err
        if len(data) != 1:
            raise exceptions.ValidationError("Invalid alert format, expected one value")
        project_id, alert_status = items[0]
        if alert_status not in [1, 0, -1]:
            raise exceptions.ValidationError("Invalid status, must be -1, 0, or 1")
        alert = alerts.filter(project_id=project_id).first()
        if alert and alert_status == -1:
            alert.delete()
        else:
            UserProjectAlert.objects.update_or_create(
                user=user, project_id=project_id, defaults={"status": alert_status}
            )
        return Response(status=204)
