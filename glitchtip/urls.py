from django.urls import path, include, re_path
from django.views.generic.base import TemplateView
from rest_framework_nested import routers
from issues.urls import router as issuesRouter
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
    path("api/", include("issues.urls")),
    path("rest-auth/", include("rest_auth.urls")),
    path("api/api-auth/", include("rest_framework.urls", namespace="rest_framework")),
    # These routes belong to the Angular single page app
    re_path(r"^$", TemplateView.as_view(template_name="index.html")),
    re_path(r"^(login|issues).*$", TemplateView.as_view(template_name="index.html")),
]
