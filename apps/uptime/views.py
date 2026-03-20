from django.core.cache import cache
from django.db.models import Q
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from django.views.generic import DetailView

from .models import Monitor, StatusPage


class StatusPageDetailView(DetailView):
    model = StatusPage

    @method_decorator(cache_control(public=True, max_age=60))
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

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
        cache_key = f"status_page_monitors:{self.object.pk}"
        monitors = cache.get(cache_key)
        if monitors is None:
            monitors = list(
                Monitor.objects.with_check_annotations().filter(
                    statuspage=self.object
                )
            )
            cache.set(cache_key, monitors, 60)
        context["monitors"] = monitors
        return context
