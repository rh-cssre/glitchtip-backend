from rest_framework_nested import routers
from .views import OrganizationViewSet

router = routers.SimpleRouter()
router.register(r"organizations", OrganizationViewSet)
