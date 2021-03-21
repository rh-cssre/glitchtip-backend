from django.contrib import admin
from .models import Notification, ProjectAlert, AlertRecipient


class NotificationAdmin(admin.ModelAdmin):
    readonly_fields = ("created", "project_alert", "is_sent", "issues")


class ProjectAlertAdmin(admin.ModelAdmin):
    search_fields = ("project__name",)
    list_display = ("project", "timespan_minutes", "quantity")
    raw_id_fields = ("project",)


class AlertRecipientAdmin(admin.ModelAdmin):
    pass


admin.site.register(Notification, NotificationAdmin)
admin.site.register(ProjectAlert, ProjectAlertAdmin)
admin.site.register(AlertRecipient, AlertRecipientAdmin)
