from django_filters import rest_framework as filters
from .models import Issue


class ListFilter(filters.Filter):
    """
    Filter that accepts multiple
    https://stackoverflow.com/a/31086033/443457
    """

    def filter(self, qs, value):
        if not value:
            return qs

        self.lookup_expr = "in"
        values = value.split(",")
        return super(ListFilter, self).filter(qs, values)


class IssueFilter(filters.FilterSet):
    project = ListFilter()

    class Meta:
        model = Issue
        fields = ["project"]

