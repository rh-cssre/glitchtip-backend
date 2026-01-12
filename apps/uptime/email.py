from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from glitchtip.email import DetailEmail

from .models import MonitorCheck

User = get_user_model()


class MonitorEmail(DetailEmail):
    html_template_name = "uptime/uptime-drip.html"
    text_template_name = "uptime/alert.txt"
    subject_template_name = "uptime/alert-subject.txt"
    model = MonitorCheck
    went_down = True
    last_change = None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        monitor = self.object.monitor
        project = getattr(monitor, "project", None)
        org = getattr(project, "organization", None) or getattr(
            monitor, "organization", None
        )
        base_url = settings.GLITCHTIP_URL.geturl()

        context["glitchtip_hostname"] = settings.GLITCHTIP_URL.hostname
        context["monitor_name"] = monitor.name
        context["monitor_url"] = monitor.url
        context["org_name"] = org.name
        context["org_slug"] = org.slug
        context["project_id"] = project.id
        context["project_name"] = project.name
        context["project_slug"] = project.slug

        context["monitor_link"] = f"{base_url}/{org.slug}/uptime-monitors/{monitor.id}"
        context["project_notification_settings_link"] = (
            f"{base_url}/{org.slug}/settings/projects/{project.slug}"
        )

        if self.object.reason:
            context["reason"] = self.object.get_reason_display

        context["start_check"] = self.object.start_check

        if self.went_down:
            context["status_msg"] = _("is down")
        else:
            context["status_msg"] = _("is back up")

        return context

    def get_users(self):
        return User.objects.uptime_monitor_recipients(self.object.monitor)
