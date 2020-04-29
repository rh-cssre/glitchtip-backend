from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from .models import User, UserProjectAlert
from .serializers import UserSerializer, UserNotificationsSerializer


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer

    def get_queryset(self):
        return super().get_queryset().filter(id=self.request.user.id)

    def get_object(self):
        pk = self.kwargs.get("pk")
        if pk == "me":
            return self.request.user
        return super().get_object()

    @action(detail=True, methods=["get", "post", "put"])
    def notifications(self, request, pk=None):
        user = self.get_object()

        if request.method == "GET":
            serializer = UserNotificationsSerializer(user)
            return Response(serializer.data)

        serializer = UserNotificationsSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        except AttributeError:
            raise ValidationError("Invalid alert format, expected dictionary")
        if len(data) != 1:
            raise ValidationError("Invalid alert format, expected one value")
        project_id, alert_status = items[0]
        if alert_status not in [1, 0, -1]:
            raise ValidationError("Invalid status, must be -1, 0, or 1")
        alert = alerts.filter(project_id=project_id).first()
        if alert and alert_status == -1:
            alert.delete()
        else:
            UserProjectAlert.objects.update_or_create(
                user=user, project_id=project_id, defaults={"status": alert_status}
            )
        return Response(status=204)
