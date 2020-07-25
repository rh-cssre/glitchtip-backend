from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

# Copy credited to OSS Sentry sentry/web/error_page_embed.py
DEFAULT_TITLE = _("It looks like we're having issues.")
GENERIC_ERROR = _(
    "An unknown error occurred while submitting your report. Please try again."
)
FORM_ERROR = _("Some fields were invalid. Please correct the errors and try again.")
SENT_MESSAGE = _("Your feedback has been sent. Thank you!")

DEFAULT_SUBTITLE = _("Our team has been notified.")
DEFAULT_SUBTITLE2 = _("If you'd like to help, tell us what happened below.")

DEFAULT_NAME_LABEL = _("Name")
DEFAULT_EMAIL_LABEL = _("Email")
DEFAULT_COMMENTS_LABEL = _("What happened?")

DEFAULT_CLOSE_LABEL = _("Close")
DEFAULT_SUBMIT_LABEL = _("Submit Crash Report")


class ErrorPageEmbedSerializer(serializers.Serializer):
    dsn = serializers.CharField()
    eventId = serializers.CharField()
    title = serializers.CharField(default=DEFAULT_TITLE)
    subtitle = serializers.CharField(default=DEFAULT_SUBTITLE)
    subtitle2 = serializers.CharField(default=DEFAULT_SUBTITLE2)
    labelName = serializers.CharField(default=DEFAULT_NAME_LABEL)
    labelEmail = serializers.CharField(default=DEFAULT_EMAIL_LABEL)
    labelComments = serializers.CharField(default=DEFAULT_COMMENTS_LABEL)
    labelClose = serializers.CharField(default=DEFAULT_CLOSE_LABEL)
    labelSubmit = serializers.CharField(default=DEFAULT_SUBMIT_LABEL)
    errorGeneric = serializers.CharField(default=GENERIC_ERROR)
    errorFormEntry = serializers.CharField(default=FORM_ERROR)
    successMessage = serializers.CharField(default=SENT_MESSAGE)
