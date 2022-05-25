from django.db.models import Avg, Count
from django_filters import rest_framework as filters

from glitchtip.filters import StartEndRelativeIsoDateTimeRangeFilter
from projects.models import Project
from .models import TransactionGroup


class TransactionGroupFilter(filters.FilterSet):
    transaction_created = StartEndRelativeIsoDateTimeRangeFilter(
        field_name="transactionevent__created", label="Transaction created",
    )
    project = filters.ModelMultipleChoiceFilter(queryset=Project.objects.all())
    query = filters.CharFilter(
        field_name="transaction",
        lookup_expr="icontains",
        label="Transaction text search",
    )

    class Meta:
        model = TransactionGroup
        fields = ["project", "transaction_created"]

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        environments = self.request.query_params.getlist("environment")
        if environments:
            queryset = queryset.filter(tags__environment__has_any_keys=environments)

        # This annotation must be applied after any related transactionevent filter
        queryset = queryset.annotate(
            avg_duration=Avg("transactionevent__duration"),
            transaction_count=Count("transactionevent"),
        )

        return queryset
