from django.contrib import admin
from django.db.models import F, Avg
from .models import TransactionGroup, TransactionEvent, Span


class TransactionGroupAdmin(admin.ModelAdmin):
    search_fields = ["title", "op", "project__organization__name"]
    list_display = ["title", "project", "op", "method", "avg_duration"]
    list_filter = ["created", "op", "method"]
    autocomplete_fields = ["project"]

    def avg_duration(self, obj):
        return obj.avg_duration

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(
            avg_duration=Avg(
                F("transactionevent__timestamp")
                - F("transactionevent__start_timestamp")
            )
        )
        return qs


class SpanInline(admin.TabularInline):
    model = Span
    extra = 0
    readonly_fields = [
        "span_id",
        "parent_span_id",
        "op",
        "description",
        "start_timestamp",
        "timestamp",
        "tags",
        "data",
    ]

    def has_add_permission(self, request, *args, **kwargs):
        return False


class TransactionEventAdmin(admin.ModelAdmin):
    search_fields = ["trace_id", "transaction", "group__project__organization__name"]
    list_display = ["trace_id", "transaction", "timestamp", "duration"]
    list_filter = ["created"]
    inlines = [SpanInline]
    can_delete = False

    def duration(self, obj):
        return obj.duration

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(duration=F("timestamp") - F("start_timestamp"))
        return qs


class SpanAdmin(admin.ModelAdmin):
    search_fields = [
        "span_id",
        "op",
        "description",
        "transaction__trace_id",
        "transaction__group__project__organization__name",
    ]
    list_display = ["span_id", "transaction", "op", "description", "duration"]
    list_filter = ["created", "op"]

    def duration(self, obj):
        return obj.duration

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(duration=F("timestamp") - F("start_timestamp"))
        return qs


admin.site.register(TransactionGroup, TransactionGroupAdmin)
admin.site.register(TransactionEvent, TransactionEventAdmin)
admin.site.register(Span, SpanAdmin)
