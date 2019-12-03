from django.urls import path, include, re_path
from django.views.generic.base import TemplateView
from rest_framework_nested import routers
from issues import urls as issuesUrls
from projects.urls import router as projectsRouter
from organizations_ext import urls as OrganizationsUrls


routeLists = [issuesUrls.routeList, OrganizationsUrls.routeList]

router = routers.DefaultRouter()
for routeList in routeLists:
    for route in routeList:
        if len(route) > 2:
            router.register(route[0], route[1], basename=route[2])
        else:
            router.register(route[0], route[1])
router.registry.extend(projectsRouter.registry)


urlpatterns = [
    path("api/0/", include(router.urls)),
    path("api/0/", include("projects.urls")),
    path("api/", include("issues.urls")),
    path("rest-auth/", include("rest_auth.urls")),
    path("api/api-auth/", include("rest_framework.urls", namespace="rest_framework")),
    # These routes belong to the Angular single page app
    re_path(r"^$", TemplateView.as_view(template_name="index.html")),
    re_path(r"^(login|issues).*$", TemplateView.as_view(template_name="index.html")),
]
