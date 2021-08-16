from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from users.models import ProjectAlertStatus
from glitchtip.email import GlitchTipEmail


User = get_user_model()


class MonitorEmail(GlitchTipEmail):
    html_template_name = "uptime/alert.txt"
    text_template_name = "uptime/alert.txt"
    subject_template_name = "uptime/alert-subject.txt"
    monitor = None
    went_down = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.went_down:
            context["status_msg"] = _("is down")
        else:
            context["status_msg"] = _("is up")
        context["name"] = self.monitor.name
        return context


def send_email_uptime_notification(monitor_check, went_down: bool):
    email = MonitorEmail()
    monitor = monitor_check.monitor
    email.monitor = monitor
    email.went_down = went_down
    users = User.objects.filter(
        organizations_ext_organization__projects__monitor=monitor
    ).exclude(
        Q(
            userprojectalert__project=monitor.project_alert.project,
            userprojectalert__status=ProjectAlertStatus.OFF,
        )
        | Q(subscribe_by_default=False, userprojectalert=None),
    )
    if not users.exists():
        return
    email.send_users_email(users)

