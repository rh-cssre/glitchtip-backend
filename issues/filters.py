from django_filters import rest_framework as filters
from projects.models import Project
from .models import Issue


class IssueFilter(filters.FilterSet):
    start = filters.IsoDateTimeFilter(field_name="created", lookup_expr="gte")
    end = filters.IsoDateTimeFilter(field_name="created", lookup_expr="lte")
    project = filters.ModelMultipleChoiceFilter(queryset=Project.objects.all())

    class Meta:
        model = Issue
        fields = ["project", "start", "end"]

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        # This exists because OSS did it this way, the astute observer will note
        # it could just as well be done in query
        environments = self.request.query_params.getlist("environment")
        if environments:
            queryset = queryset.filter(tags__environment__has_any_keys=environments)
        return queryset
