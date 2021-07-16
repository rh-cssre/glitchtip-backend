import json
from django.db import IntegrityError
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from rest_framework import views, viewsets, exceptions, permissions, renderers
from rest_framework.response import Response

from projects.models import ProjectKey
from events.models import Event
from issues.permissions import EventPermission
from .models import UserReport
from .forms import UserReportForm
from .serializers import ErrorPageEmbedSerializer, UserReportSerializer


class JavaScriptSuccessRenderer(renderers.JSONRenderer):
    """
    Render as JavaScript when status code is 200.
    On failure status codes, render as JSON instead.
    """

    media_type = "application/javascript"
    format = "js"

    def render(self, data, media_type=None, renderer_context=None):
        if renderer_context["response"].status_code == 200:
            charset = "utf-8"
            return data.encode(charset)
        return super().render(data, "application/json", renderer_context)


class ErrorPageEmbedView(views.APIView):
    """
    Ideas taken from OSS Sentry sentry/web/error_page_embed.py
    Modified for DRF
    """

    permission_classes = [permissions.AllowAny]
    renderer_classes = [
        renderers.JSONRenderer,
        renderers.BrowsableAPIRenderer,
        JavaScriptSuccessRenderer,
    ]

    def get_format_suffix(self, **kwargs):
        if self.request.method == "GET":
            return "js"
        return super().get_format_suffix(**kwargs)

    def process_get_params(self, params):
        serializer = ErrorPageEmbedSerializer(data=params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def get_project_key(self, validated_data):
        dsn = validated_data["dsn"]
        try:
            return ProjectKey.from_dsn(dsn)
        except ProjectKey.DoesNotExist:
            raise exceptions.NotFound("Invalid dsn parameter")

    def get(self, request):
        data = self.process_get_params(request.GET)
        self.get_project_key(data)

        initial = {"name": request.GET.get("name"), "email": request.GET.get("email")}

        # Stubbed, should be configurable
        show_branding = True

        form = UserReportForm(initial=initial)
        template = render_to_string(
            "user_reports/error-page-embed.html",
            {
                "form": form,
                "show_branding": show_branding,
                "title": data["title"],
                "subtitle": data["subtitle"],
                "subtitle2": data["subtitle2"],
                "name_label": data["labelName"],
                "email_label": data["labelEmail"],
                "comments_label": data["labelComments"],
                "submit_label": data["labelSubmit"],
                "close_label": data["labelClose"],
            },
        )

        url = settings.GLITCHTIP_URL.geturl() + request.get_full_path()

        context = {
            "endpoint": mark_safe("*/" + json.dumps(url) + ";/*"),
            "template": mark_safe("*/" + json.dumps(template) + ";/*"),
            "strings": mark_safe(
                json.dumps(
                    {
                        "generic_error": str(data["errorGeneric"]),
                        "form_error": str(data["errorFormEntry"]),
                        "sent_message": str(data["successMessage"]),
                    }
                )
            ),
        }

        return Response(
            render_to_string("user_reports/error-page-embed.js", context, request),
            content_type="application/javascript",
        )

    def post(self, request):
        data = self.process_get_params(request.GET)
        project_key = self.get_project_key(data)
        event_id = data["eventId"]
        initial = {"name": request.GET.get("name"), "email": request.GET.get("email")}
        form = UserReportForm(request.POST, initial=initial)
        if form.is_valid():
            report = form.save(commit=False)
            report.project = project_key.project
            report.event_id = event_id
            event = Event.objects.filter(event_id=event_id).first()
            if event:
                report.issue = event.issue
            try:
                report.save()
            except IntegrityError:
                pass  # Duplicate, ignore
            return Response()
        return Response({"errors": form.errors}, status=400)


class UserReportViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = UserReport.objects.all()
    serializer_class = UserReportSerializer
    permission_classes = [EventPermission]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = super().get_queryset()
        queryset = queryset.filter(project__team__members__user=self.request.user)
        issue_id = self.kwargs.get("issue_pk")
        if issue_id:
            queryset = queryset.filter(issue_id=issue_id)
        return queryset
