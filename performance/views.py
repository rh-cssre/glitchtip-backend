from rest_framework import viewsets
from projects.models import Project
from .models import TransactionEvent, TransactionGroup, Span
from .serializers import (
    TransactionSerializer,
    TransactionGroupSerializer,
    SpanSerializer,
)


class TransactionGroupViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransactionGroup.objects.all()
    serializer_class = TransactionGroupSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        # Performance optimization, force two queries
        projects = list(
            Project.objects.filter(team__members__user=self.request.user).values_list(
                "pk", flat=True
            )
        )
        qs = super().get_queryset().filter(project__pk__in=projects)
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                project__organization__slug=self.kwargs["organization_slug"],
            )
        return qs


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransactionEvent.objects.all()
    serializer_class = TransactionSerializer

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
