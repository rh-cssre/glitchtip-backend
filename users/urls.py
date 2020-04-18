from django.urls import path, include
from glitchtip.routers import BulkSimpleRouter
from .views import UserViewSet

router = BulkSimpleRouter()
router.register(r"users", UserViewSet)


urlpatterns = [path("", include(router.urls))]
