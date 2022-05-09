import logging
import urllib.parse as urlparse
from urllib.parse import parse_qs

from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class LinkHeaderPagination(CursorPagination):
    """Inform the user of pagination links via response headers, similar to
    what's described in
    https://developer.github.com/guides/traversing-with-pagination/.
    """

    page_size_query_param = "limit"
    max_hits = 1000

    def paginate_queryset(self, queryset, request, view=None):
        self.count = self.get_count(queryset)
        try:
            return super().paginate_queryset(queryset, request, view)
        except ValueError as err:
            # https://gitlab.com/glitchtip/glitchtip-backend/-/issues/136
            logging.warning("Pagination received invalid cursor", exc_info=True)
            raise ValidationError("Invalid page cursor") from err

    def get_count(self, queryset):
        """Count with max limit, to prevent slowdown"""
        return queryset[: self.max_hits].count()

    def get_paginated_response(self, data):
        next_url = self.get_next_link()
        previous_url = self.get_previous_link()

        links = []
        for url, label in (
            (previous_url, "previous"),
            (next_url, "next"),
        ):
            if url is not None:
                parsed = urlparse.urlparse(url)
                cursor = parse_qs(parsed.query).get(self.cursor_query_param, [""])[0]
                links.append(
                    '<{}>; rel="{}"; results="true"; cursor="{}"'.format(
                        url, label, cursor
                    )
                )
            else:
                links.append(
                    '<{}>; rel="{}"; results="false"'.format(self.base_url, label)
                )

        headers = {"Link": ", ".join(links)} if links else {}

        headers["X-Max-Hits"] = self.max_hits
        headers["X-Hits"] = self.count

        return Response(data, headers=headers)
