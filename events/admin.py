from django.contrib import admin
from .models import Event


class EventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "created")
    list_filter = ("created",)
    raw_id_fields = ("issue",)
    search_fields = ("event_id",)
    show_full_result_count = False


admin.site.register(Event, EventAdmin)
