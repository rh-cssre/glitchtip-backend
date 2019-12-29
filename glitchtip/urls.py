from django.conf import settings
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from rest_framework_nested import routers
from issues.urls import router as issuesRouter, store_urlpatterns
from projects.urls import router as projectsRouter
from organizations_ext.urls import router as organizationsRouter


router = routers.DefaultRouter()
router.registry.extend(projectsRouter.registry)
router.registry.extend(issuesRouter.registry)
router.registry.extend(organizationsRouter.registry)

urlpatterns = [
    path("api/0/", include(router.urls)),
    path("api/0/", include("projects.urls")),
    path("api/0/", include("issues.urls")),
    path("api/0/", include("organizations_ext.urls")),
    path("api/", include(store_urlpatterns)),
    path("rest-auth/", include("rest_auth.urls")),
    path("api/api-auth/", include("rest_framework.urls", namespace="rest_framework")),
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

