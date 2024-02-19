from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, status


class ConflictException(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = _("Already present!")
    default_code = "already_present"


class ServiceUnavailableException(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Service temporarily unavailable, try again later."
    default_code = "service_unavailable"
