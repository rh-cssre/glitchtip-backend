import json
from typing import Optional
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from ninja import Field, Form, Query, Router
from ninja.errors import AuthenticationError

from apps.issue_events.models import IssueEvent
from apps.projects.models import Project
from glitchtip.schema import CamelSchema

from .forms import UserReportForm


async def embed_auth(request: HttpRequest):
    dsn = request.GET.get("dsn")
    urlparts = urlparse(dsn)
    public_key = urlparts.username
    path = str(urlparts.path)
    project_id = path.rsplit("/", 1)[-1]
    try:
        project = (
            await Project.objects.filter(
                id=project_id, projectkey__public_key=public_key
            )
            .select_related("organization")
            .only("id", "organization__event_throttle_rate")
            .aget()
        )
    except ValueError:
        raise AuthenticationError([{"message": "Invalid DSN"}])
    if project.organization.event_throttle_rate == 100:
        raise AuthenticationError([{"message": "Invalid DSN"}])
    return project


class EmbedAuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by the embed api"""

    auth: Project


router = Router(auth=embed_auth)


# Copy credited to OSS Sentry sentry/web/error_page_embed.py
DEFAULT_TITLE = "It looks like we're having issues."
GENERIC_ERROR = (
    "An unknown error occurred while submitting your report. Please try again."
)
FORM_ERROR = "Some fields were invalid. Please correct the errors and try again."
SENT_MESSAGE = "Your feedback has been sent. Thank you!"
DEFAULT_SUBTITLE = "Our team has been notified."
DEFAULT_SUBTITLE2 = "If you'd like to help, tell us what happened below."
DEFAULT_NAME_LABEL = "Name"
DEFAULT_EMAIL_LABEL = "Email"
DEFAULT_COMMENTS_LABEL = "What happened?"
DEFAULT_CLOSE_LABEL = "Close"
DEFAULT_SUBMIT_LABEL = "Submit Crash Report"


class EmbedSchema(CamelSchema):
    dsn: str
    eventId: str = Field()


class EmbedGetSchema(EmbedSchema):
    title: str = DEFAULT_TITLE
    subtitle: str = DEFAULT_SUBTITLE
    subtitle2: str = DEFAULT_SUBTITLE2
    label_name: str = DEFAULT_NAME_LABEL
    label_email: str = DEFAULT_EMAIL_LABEL
    label_comments: str = DEFAULT_COMMENTS_LABEL
    label_close: str = DEFAULT_CLOSE_LABEL
    label_submit: str = DEFAULT_SUBMIT_LABEL
    error_generic: str = GENERIC_ERROR
    error_form_entry: str = FORM_ERROR
    success_message: str = SENT_MESSAGE
    name: Optional[str] = None
    email: Optional[str] = None


class UserReportFormInput(CamelSchema):
    name: Optional[str]
    email: Optional[str]
    comments: Optional[str]


@router.get("/error-page/")
async def get_embed_error_page(request: HttpRequest, data: Query[EmbedGetSchema]):
    initial = {"name": data.name, "email": data.email}

    # Stubbed, should be configurable
    show_branding = True

    form = UserReportForm(initial=initial)
    template = render_to_string(
        "user_reports/error-page-embed.html",
        {
            "form": form,
            "show_branding": show_branding,
            "title": data.title,
            "subtitle": data.subtitle,
            "subtitle2": data.subtitle2,
            "name_label": data.label_name,
            "email_label": data.label_email,
            "comments_label": data.label_comments,
            "submit_label": data.label_submit,
            "close_label": data.label_close,
        },
    )

    url = settings.GLITCHTIP_URL.geturl() + request.get_full_path()

    context = {
        "endpoint": mark_safe("*/" + json.dumps(url) + ";/*"),
        "template": mark_safe("*/" + json.dumps(template) + ";/*"),
        "strings": mark_safe(
            json.dumps(
                {
                    "generic_error": data.error_generic,
                    "form_error": data.error_form_entry,
                    "sent_message": data.success_message,
                }
            )
        ),
    }

    return HttpResponse(
        render_to_string("user_reports/error-page-embed.js", context, request),
        content_type="application/javascript",
    )


@router.post("/error-page/")
async def submit_embed_error_page(
    request: EmbedAuthHttpRequest,
    data: Query[EmbedGetSchema],
    form: Form[UserReportFormInput],
):
    event_id = data.eventId
    initial = {"name": data.name, "email": data.email}
    form = UserReportForm(form.dict(), initial=initial)
    if form.is_valid():
        report = form.save(commit=False)
        report.project_id = request.auth.id
        report.event_id = event_id
        event = await IssueEvent.objects.filter(id=event_id).afirst()
        if event:
            report.issue_id = event.issue_id
        try:
            await report.asave()
        except IntegrityError:
            pass  # Duplicate, ignore
        return HttpResponse()
    return HttpResponse(
        json.dumps({"errors": form.errors}),
        status=400,
        content_type="application/json",
    )
