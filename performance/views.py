from rest_framework import viewsets
from rest_framework.filters import OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from projects.models import Project
from .models import TransactionEvent, TransactionGroup, Span
from .serializers import (
    TransactionSerializer,
    TransactionDetailSerializer,
    TransactionGroupSerializer,
    SpanSerializer,
)
from .filters import TransactionGroupFilter


class TransactionGroupViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransactionGroup.objects.all()
    serializer_class = TransactionGroupSerializer
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = TransactionGroupFilter
    ordering = ["-avg_duration"]
    ordering_fields = ["created", "avg_duration", "transaction_count"]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        # Performance optimization, force two queries
        projects = Project.objects.filter(team__members__user=self.request.user)
        if "organization_slug" in self.kwargs:
            projects = projects.filter(
                organization__slug=self.kwargs["organization_slug"],
            )

        qs = (
            super()
            .get_queryset()
            .filter(project__pk__in=list(projects.values_list("pk", flat=True)))
            .defer("search_vector", "created")
        )

        return qs


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransactionEvent.objects.all()
    serializer_class = TransactionSerializer

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return TransactionDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        # Performance optimization, force two queries
        projects = list(
            Project.objects.filter(team__members__user=self.request.user).values_list(
                "pk", flat=True
            )
        )
        qs = super().get_queryset().filter(group__project__pk__in=projects)
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                group__project__organization__slug=self.kwargs["organization_slug"],
            )
        qs = qs.select_related("group")
        return qs


class SpanViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Span.objects.all()
    serializer_class = SpanSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        # Performance optimization, force two queries
        projects = list(
            Project.objects.filter(team__members__user=self.request.user).values_list(
                "pk", flat=True
            )
        )
        qs = super().get_queryset().filter(transaction__group__project__pk__in=projects)
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                transaction__group__project__organization__slug=self.kwargs[
                    "organization_slug"
                ],
            )
        return qs
