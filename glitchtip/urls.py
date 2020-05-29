from django.conf import settings
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from dj_rest_auth.registration.views import SocialAccountDisconnectView
from rest_framework import permissions
from rest_framework_nested import routers
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from issues.urls import router as issuesRouter
from issues.views import EventJsonView
from projects.urls import router as projectsRouter
from teams.urls import router as teamsRouter
from organizations_ext.urls import router as organizationsRouter
from users.urls import router as usersRouter
from . import social
from .yasg import CustomOpenAPISchemaGenerator
from .views import SettingsView, health


router = routers.DefaultRouter()
router.registry.extend(projectsRouter.registry)
router.registry.extend(issuesRouter.registry)
router.registry.extend(organizationsRouter.registry)
router.registry.extend(teamsRouter.registry)
router.registry.extend(usersRouter.registry)

if settings.BILLING_ENABLED:
    from djstripe_ext.urls import router as djstripeRouter

    router.registry.extend(djstripeRouter.registry)


schema_view = get_schema_view(
    openapi.Info(
        title="GlitchTip API",
        default_version="v1",
        description="GlitchTip Backend API",
        terms_of_service="https://glitchtip.com",
        contact=openapi.Contact(email="info@burkesoftware.com"),
        license=openapi.License(name="MIT License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
    generator_class=CustomOpenAPISchemaGenerator,
)


urlpatterns = [
    path("_health/", health),
    path("admin/", admin.site.urls),
    path("api/0/", include(router.urls)),
]

if settings.BILLING_ENABLED:
    urlpatterns += [
        path("api/0/", include("djstripe_ext.urls")),
    ]

urlpatterns += [
    path("api/0/", include("projects.urls")),
    path("api/0/", include("issues.urls")),
    path("api/0/", include("organizations_ext.urls")),
    path("api/0/", include("teams.urls")),
    path("api/", include("event_store.urls")),
    # What an oddball API endpoint
    path(
        "organizations/<slug:org>/issues/<int:issue>/events/<str:event>/json/",
        EventJsonView.as_view(),
        name="event_json",
    ),
    path("api/settings/", SettingsView.as_view(), name="settings"),
    path("rest-auth/", include("dj_rest_auth.urls")),
    path("rest-auth/registration/", include("dj_rest_auth.registration.urls")),
    path("api/api-auth/", include("rest_framework.urls", namespace="rest_framework")),
    re_path(
        r"^socialaccounts/(?P<pk>\d+)/disconnect/$",
        SocialAccountDisconnectView.as_view(),
        name="social_account_disconnect",
    ),
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
    path("docs/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"),
    path("accounts/", include("allauth.urls")),  # Required for allauth
    # These routes belong to the Angular single page app
    re_path(r"^$", TemplateView.as_view(template_name="index.html")),
    re_path(
        r"^(login|issues|settings|organizations|profile).*$",
        TemplateView.as_view(template_name="index.html"),
    ),
]

if settings.BILLING_ENABLED:
    urlpatterns.append(path("stripe/", include("djstripe.urls", namespace="djstripe")))

if settings.ENABLE_TEST_API:
    urlpatterns.append(path("api/test/", include("test_api.urls")))

if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
        # For django versions before 2.0:
        # url(r'^__debug__/', include(debug_toolbar.urls)),
    ] + urlpatterns
