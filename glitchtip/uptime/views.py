from django.shortcuts import get_object_or_404
from rest_framework import permissions, viewsets, exceptions
from rest_framework.generics import CreateAPIView

from organizations_ext.models import Organization
from .models import Monitor
from .serializers import HeartBeatCheckSerializer, MonitorSerializer
from .tasks import send_monitor_notification


class HeartBeatCheckView(CreateAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = HeartBeatCheckSerializer

    def perform_create(self, serializer):
        monitor = get_object_or_404(
            Monitor.objects.with_check_annotations(),
            organization__slug=self.kwargs.get("organization_slug"),
            endpoint_id=self.kwargs.get("endpoint_id"),
        )
        monitor_check = serializer.save(monitor=monitor, is_up=True)
        if monitor.latest_is_up is False:
            send_monitor_notification.delay(
                monitor_check.pk, False, monitor.last_change
            )


class MonitorViewSet(viewsets.ModelViewSet):
    queryset = Monitor.objects.with_check_annotations()
    serializer_class = MonitorSerializer

    def get_queryset(self):
        print("querying")
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        return queryset

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug")
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")
        serializer.save(organization=organization)

    #     except Organization.DoesNotExist:
    #         raise exceptions.ValidationError("Organization does not exist")
    #     serializer.save(organization=organization)

        #    def perform_create(self, serializer):
        # try:
        #     project = Project.objects.get(
        #         slug=self.kwargs.get("project_slug"),
        #         team__members__user=self.request.user,
        #         organization__slug=self.kwargs.get("organization_slug"),
        #     )
        # except Project.DoesNotExist:
        #     raise exceptions.ValidationError("Organization does not exist")
        # serializer.save(project=project)
