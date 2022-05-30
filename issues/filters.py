from django_filters import rest_framework as filters

from glitchtip.filters import StartEndRelativeIsoDateTimeRangeFilter
from projects.models import Project
from .models import Issue


class IssueFilter(filters.FilterSet):
    created = StartEndRelativeIsoDateTimeRangeFilter(
        field_name="created", label="Issue created",
    )
    project = filters.ModelMultipleChoiceFilter(queryset=Project.objects.all())

    class Meta:
        model = Issue
        fields = ["project", "created"]

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        # This exists because OSS did it this way, the astute observer will note
        # it could just as well be done in query
        environments = self.request.query_params.getlist("environment")
        if environments:
            queryset = queryset.filter(tags__environment__has_any_keys=environments)
        return queryset
