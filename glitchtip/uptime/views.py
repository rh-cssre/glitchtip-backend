from django.db.models import OuterRef, Prefetch, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import exceptions, permissions, viewsets
from rest_framework.generics import CreateAPIView

from organizations_ext.models import Organization

from .models import Monitor, MonitorCheck
from .serializers import (
    HeartBeatCheckSerializer,
    MonitorCheckSerializer,
    MonitorSerializer,
)
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
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)

        subqueryset = Subquery(
            MonitorCheck.objects.filter(
                monitor=OuterRef("monitor")
            ).order_by("-start_check").values_list("id", flat=True)[:60]
        )

        # Optimization hack, we know the checks will be recent. No need to sort all checks ever.
        hours_ago = timezone.now() - timezone.timedelta(hours=12)
        queryset = queryset.prefetch_related(
            Prefetch(
                "checks",
                queryset=MonitorCheck.objects.filter(
                    id__in=subqueryset, start_check__gt=hours_ago
                ).order_by("-start_check"),
            )
        ).select_related("project").distinct()
        return queryset

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"), users=self.request.user
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization not found")
        serializer.save(organization=organization)


class MonitorCheckViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MonitorCheck.objects.all()
    serializer_class = MonitorCheckSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(monitor__organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(monitor__organization__slug=organization_slug)
        monitor_pk = self.kwargs.get("monitor_pk")
        if monitor_pk:
            queryset = queryset.filter(monitor__pk=monitor_pk)
        return queryset
