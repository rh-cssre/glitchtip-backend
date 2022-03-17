from django.contrib import admin
from .models import TransactionGroup, TransactionEvent, Span


class TransactionGroupAdmin(admin.ModelAdmin):
    search_fields = ["title", "op"]
    list_filter = ["created", "op", "method"]


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
    search_fields = ["trace_id", "transaction"]
    list_filter = ["created"]
    inlines = [SpanInline]
    can_delete = False


class SpanAdmin(admin.ModelAdmin):
    search_fields = ["span_id", "op", "description", "transaction__trace_id"]
    list_filter = ["created"]


admin.site.register(TransactionGroup, TransactionGroupAdmin)
admin.site.register(TransactionEvent, TransactionEventAdmin)
admin.site.register(Span, SpanAdmin)
