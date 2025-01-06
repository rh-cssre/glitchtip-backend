from urllib import parse

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.db.models import Exists, OuterRef
from django.utils.translation import gettext_lazy as _

from apps.projects.models import ProjectAlertStatus


class UserManager(BaseUserManager):
    """
    A custom user manager to deal with emails as unique identifiers for auth
    instead of usernames. The default that's used is "UserManager"
    """

    def create_user(self, email, password, **extra_fields):
        """
        Creates and saves a User with the given email and password.
        """
        if not email:
            raise ValueError("The Email must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)

    def alert_notification_recipients(self, notification):
        """Distinct users associated with a project notification who should receive alerts"""
        queryset = self.filter(
            organizations_ext_organizationuser__teams__projects__projectalert__notification=notification
        )
        return self._exclude_recipients(queryset, notification.project_alert.project)

    def uptime_monitor_recipients(self, monitor):
        """Distinct users associated with a project uptime monitor who should receive alerts"""
        queryset = self.filter(
            organizations_ext_organizationuser__teams__projects__monitor=monitor
        )
        return self._exclude_recipients(queryset, monitor.project)

    def _exclude_recipients(self, queryset, project):
        """Exclude from queryset users who have a preference not to receive notifications"""
        from apps.projects.models import UserProjectAlert

        explicit_off = UserProjectAlert.objects.filter(
            user=OuterRef("pk"), project=project, status=ProjectAlertStatus.OFF
        )

        has_project_alert = UserProjectAlert.objects.filter(
            user=OuterRef("pk"), project=project
        )

        return queryset.exclude(
            Exists(explicit_off)
            | (~Exists(has_project_alert) & ~models.Q(subscribe_by_default=True))
        ).distinct()


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True, db_collation="case_insensitive")
    name = models.CharField(_("name"), max_length=255, blank=True)
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Designates whether the user can log into this site."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting accounts."
        ),
    )
    analytics = models.JSONField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    subscribe_by_default = models.BooleanField(
        default=True,
        help_text="Subscribe to project notifications by default. Overrides project settings",
    )
    options = models.JSONField(default=dict)
    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    objects = UserManager()

    def __str__(self):
        return self.email

    def get_full_name(self):
        return self.email

    def get_short_name(self):
        return self.email

    @property
    def username(self):
        return self.email

    @property
    def auth_token(self):
        return None

    def set_register_analytics_tags(self, tags: str):
        """
        Set UTM querystring to user's analytics field
        """
        parsed_tags = parse.parse_qsl(tags.strip("?"))
        if self.analytics is None:
            self.analytics = {}
        self.analytics["register"] = {
            tag[0]: tag[1] for tag in parsed_tags if tag[0].startswith("utm_")
        }
