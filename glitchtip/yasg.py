from django.conf import settings
from drf_yasg.generators import EndpointEnumerator, OpenAPISchemaGenerator
from drf_yasg.inspectors import SwaggerAutoSchema


# Work around incompatibility with dj rest auth endpoint
# https://github.com/axnsan12/drf-yasg/issues/435
class CustomEndpointEnumerator(EndpointEnumerator):
    """
    Add custom setting to exclude views
    """

    def should_include_endpoint(
        self, path, callback, app_name="", namespace="", url_name=None
    ):
        view = "{}.{}".format(callback.__module__, callback.__qualname__)
        if view in settings.DRF_YASG_EXCLUDE_VIEWS:
            return False
        return super().should_include_endpoint(
            path, callback, app_name, namespace, url_name
        )


class CustomOpenAPISchemaGenerator(OpenAPISchemaGenerator):
    """
    We want change default endpoint enumerator class
    """

    endpoint_enumerator_class = CustomEndpointEnumerator


# Work around to group viewset views together instead of on giant "api" group
# https://github.com/axnsan12/drf-yasg/issues/489
class SquadSwaggerAutoSchema(SwaggerAutoSchema):
    def get_tags(self, operation_keys=None):
        tags = super().get_tags(operation_keys)

        if "api" in tags and operation_keys:
            # NOTE: `operation_keys` is a list like ["api", "v1", "token", "read"].
            tags[0] = operation_keys[2]

        return tags
