from django.contrib import admin

from .models import AlertRecipient, Notification, ProjectAlert


class NotificationAdmin(admin.ModelAdmin):
    readonly_fields = ("created", "project_alert", "is_sent", "issues")


class AlertRecipientInline(admin.TabularInline):
    model = AlertRecipient
    extra = 0


class ProjectAlertAdmin(admin.ModelAdmin):
    search_fields = ("project__name",)
    list_display = ("project", "timespan_minutes", "quantity")
    raw_id_fields = ("project",)
    inlines = [AlertRecipientInline]


admin.site.register(Notification, NotificationAdmin)
admin.site.register(ProjectAlert, ProjectAlertAdmin)
