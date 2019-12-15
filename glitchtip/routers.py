from rest_framework.routers import Route
from rest_framework_nested import routers


class BulkSimpleRouter(routers.SimpleRouter):
    """
    Router supports PUT method on list view to support bulk updates
    Thanks to Github user thomasWajs
    https://github.com/miki725/django-rest-framework-bulk/issues/11#issuecomment-45742375
    Fun fact. I, bufke, first opened the question about this in 2014!
    """

    routes = routers.SimpleRouter.routes
    routes[0] = Route(
        url=r"^{prefix}{trailing_slash}$",
        mapping={"get": "list", "post": "create", "put": "bulk_update",},
        name="{basename}-list",
        detail=False,
        initkwargs={"suffix": "List"},
    )
