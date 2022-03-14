from django.urls import path

from .views import DjangoPrometheusMetrics


urlpatterns = [
    path("django/", DjangoPrometheusMetrics.as_view(), name="prometheus-django-metrics")
]

