from django.conf import settings
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from rest_framework_nested import routers
from issues.urls import router as issuesRouter
from projects.urls import router as projectsRouter
from organizations_ext.urls import router as organizationsRouter
from . import social
from .views import SettingsView, health


router = routers.DefaultRouter()
router.registry.extend(projectsRouter.registry)
router.registry.extend(issuesRouter.registry)
router.registry.extend(organizationsRouter.registry)

urlpatterns = [
    path("_health/", health),
    path("admin/", admin.site.urls),
    path("api/0/", include(router.urls)),
    path("api/0/", include("projects.urls")),
    path("api/0/", include("issues.urls")),
    path("api/0/", include("organizations_ext.urls")),
    path("api/", include("event_store.urls")),
    path("api/settings/", SettingsView.as_view(), name="settings"),
    path("rest-auth/", include("rest_auth.urls")),
    path("api/api-auth/", include("rest_framework.urls", namespace="rest_framework")),
    path("rest-auth/gitlab/", social.GitlabLogin.as_view(), name="gitlab_login"),
    path(
        "rest-auth/gitlab/connect/",
        social.GitlabConnect.as_view(),
        name="gitlab_connect",
    ),
    path("rest-auth/github/", social.GithubLogin.as_view(), name="github_login"),
    path(
        "rest-auth/github/connect/",
        social.GithubConnect.as_view(),
        name="github_connect",
    ),
    path("rest-auth/google/", social.GoogleLogin.as_view(), name="google_login"),
    path(
        "rest-auth/google/connect/",
        social.GoogleConnect.as_view(),
        name="google_connect",
    ),
    path(
        "rest-auth/microsoft/", social.MicrosoftLogin.as_view(), name="microsoft_login"
    ),
    path(
        "rest-auth/microsoft/connect/",
        social.MicrosoftConnect.as_view(),
        name="microsoft_connect",
    ),
    path("accounts/", include("allauth.urls")),  # Required for allauth
    # These routes belong to the Angular single page app
    re_path(r"^$", TemplateView.as_view(template_name="index.html")),
    re_path(
        r"^(login|issues|settings).*$", TemplateView.as_view(template_name="index.html")
    ),
]
if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
        # For django versions before 2.0:
        # url(r'^__debug__/', include(debug_toolbar.urls)),
    ] + urlpatterns

