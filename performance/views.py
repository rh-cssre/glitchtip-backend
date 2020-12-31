from rest_framework import viewsets
from .models import TransactionEvent
from .serializers import TransactionSerializer


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TransactionEvent.objects.all()
    serializer_class = TransactionSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        qs = (
            super()
            .get_queryset()
            .filter(project__team__members__user=self.request.user)
        )
        return qs
