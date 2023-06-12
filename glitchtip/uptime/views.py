from django.db.models import F, Prefetch, Window
from django.db.models.functions import RowNumber
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import exceptions, permissions, viewsets
from rest_framework.generics import CreateAPIView

from glitchtip.pagination import LinkHeaderPagination
from organizations_ext.models import Organization

from .models import Monitor, MonitorCheck
from .serializers import (
    HeartBeatCheckSerializer,
    MonitorCheckSerializer,
    MonitorDetailSerializer,
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
        monitor_check = serializer.save(monitor=monitor, is_up=True, reason=None)
        if monitor.latest_is_up is False:
            send_monitor_notification.delay(
                monitor_check.pk, False, monitor.last_change
            )


class MonitorViewSet(viewsets.ModelViewSet):
    queryset = Monitor.objects.with_check_annotations()
    serializer_class = MonitorSerializer

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return MonitorDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)

        # Fetch latest 60 checks for each monitor
        queryset = queryset.prefetch_related(
            Prefetch(
                "checks",
                queryset=MonitorCheck.objects.filter(  # Optimization
                    start_check__gt=timezone.now() - timezone.timedelta(hours=12)
                )
                .annotate(
                    row_number=Window(
                        expression=RowNumber(),
                        order_by="-start_check",
                        partition_by=F("monitor"),
                    ),
                )
                .filter(row_number__lte=60)
                .distinct(),
            )
        ).select_related("project")
        return queryset

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"), users=self.request.user
            )
        except Organization.DoesNotExist as exc:
            raise exceptions.ValidationError("Organization not found") from exc
        serializer.save(organization=organization)


class MonitorCheckPagination(LinkHeaderPagination):
    ordering = "-start_check"


class MonitorCheckViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MonitorCheck.objects.all()
    serializer_class = MonitorCheckSerializer
    pagination_class = MonitorCheckPagination
    filterset_fields = ["is_change"]

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
        return queryset.only("is_up", "start_check", "reason", "response_time")
