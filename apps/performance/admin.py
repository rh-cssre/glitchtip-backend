from django.contrib import admin
from django.db.models import Avg

from .models import TransactionGroup


class TransactionGroupAdmin(admin.ModelAdmin):
    search_fields = ["transaction", "op", "project__organization__name"]
    list_display = ["transaction", "project", "op", "method", "avg_duration"]
    list_filter = ["created", "op", "method"]
    autocomplete_fields = ["project"]

    def avg_duration(self, obj):
        return obj.avg_duration

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(avg_duration=Avg("transactionevent__duration"))
        )


admin.site.register(TransactionGroup, TransactionGroupAdmin)
