from django.urls import path, include

urlpatterns = [
    path("api/", include("issues.urls")),
    path("rest-auth/", include("rest_auth.urls")),
    # These routes belong to the Angular single page app
    url(r'^$', TemplateView.as_view(template_name='index.html')),
    url(r'^(login|issues).*$', TemplateView.as_view(template_name='index.html')),
]
