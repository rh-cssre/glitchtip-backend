from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from glitchtip.email import DetailEmail
from users.models import ProjectAlertStatus, User

from .models import MonitorCheck

User = get_user_model()


class MonitorEmail(DetailEmail):
    html_template_name = "uptime/alert.txt"
    text_template_name = "uptime/alert.txt"
    subject_template_name = "uptime/alert-subject.txt"
    model = MonitorCheck
    went_down = True
    last_change = None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.went_down:
            context["status_msg"] = _("is down")
        else:
            context["status_msg"] = _("is up")
        if self.last_change:
            context["time_since_last_change"] = timezone.now() - self.last_change
        return context

    def get_users(self):
        monitor = self.object.monitor
        return User.objects.filter(
            organizations_ext_organization__projects__monitor=monitor
        ).exclude(
            Q(
                userprojectalert__project=monitor.project,
                userprojectalert__status=ProjectAlertStatus.OFF,
            )
            | Q(subscribe_by_default=False, userprojectalert=None),
        )
