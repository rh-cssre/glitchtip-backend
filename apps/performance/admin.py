from django.contrib import admin
from django.db.models import Avg

from .models import TransactionEvent, TransactionGroup


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


# class SpanInline(admin.TabularInline):
#     model = Span
#     extra = 0
#     readonly_fields = [
#         "span_id",
#         "parent_span_id",
#         "op",
#         "description",
#         "start_timestamp",
#         "timestamp",
#         "tags",
#         "data",
#     ]

#     def has_add_permission(self, request, *args, **kwargs):
#         return False


class TransactionEventAdmin(admin.ModelAdmin):
    search_fields = [
        "trace_id",
        "group__transaction",
        "group__project__organization__name",
    ]
    list_display = ["trace_id", "group", "timestamp", "duration"]
    # inlines = [SpanInline]
    can_delete = False


admin.site.register(TransactionGroup, TransactionGroupAdmin)
admin.site.register(TransactionEvent, TransactionEventAdmin)
