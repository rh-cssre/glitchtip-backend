from django.contrib import admin
from .models import Notification, ProjectAlert


class NotificationAdmin(admin.ModelAdmin):
    readonly_fields = ("created", "project", "is_sent", "issues")


class ProjectAlertAdmin(admin.ModelAdmin):
    search_fields = ("project__name",)
    list_display = ("project", "timespan_minutes", "quantity")
    raw_id_fields = ("project",)


admin.site.register(Notification, NotificationAdmin)
admin.site.register(ProjectAlert, ProjectAlertAdmin)
