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
