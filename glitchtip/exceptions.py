from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, status


class ConflictException(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = _("Already present!")
    default_code = "already_present"
