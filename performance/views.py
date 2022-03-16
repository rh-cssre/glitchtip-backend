from rest_framework import viewsets
from projects.models import Project
from .models import TransactionEvent
from .serializers import TransactionSerializer


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
