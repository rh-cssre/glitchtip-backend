from django.urls import path, include, re_path
from django.views.generic.base import TemplateView


urlpatterns = [
    path("api/", include("issues.urls")),
    path("rest-auth/", include("rest_auth.urls")),
    # These routes belong to the Angular single page app
    re_path(r'^$', TemplateView.as_view(template_name='index.html')),
    re_path(r'^(login|issues).*$', TemplateView.as_view(template_name='index.html')),
]
