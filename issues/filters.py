from django_filters import rest_framework as filters
from .models import Issue


class ListFilter(filters.Filter):
    """
    Filter that accepts multiple in format of ?foo=1&foo=2
    """

    def filter(self, qs, value):
        if not value:
            return qs

        self.lookup_expr = "in"
        # getlist is necessary to get all values from the query dict
        values = self.parent.data.getlist(self.field_name)
        return super().filter(qs, values)


class IssueFilter(filters.FilterSet):
    start = filters.IsoDateTimeFilter(field_name="created", lookup_expr="gte")
    end = filters.IsoDateTimeFilter(field_name="created", lookup_expr="lte")
    project = ListFilter()

    class Meta:
        model = Issue
        fields = ["project", "start", "end"]
