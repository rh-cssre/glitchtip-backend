from django.db.models import Q
from django.views.generic import DetailView

from .models import Monitor, StatusPage


class StatusPageDetailView(DetailView):
    model = StatusPage

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_authenticated:
            queryset = queryset.filter(
                Q(is_public=True) | Q(organization__users=self.request.user)
            )
        else:
            queryset = queryset.filter(is_public=True)

        return queryset.filter(
            organization__slug=self.kwargs.get("organization")
        ).distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["monitors"] = Monitor.objects.with_check_annotations().filter(
            statuspage=self.object
        )
        return context
