from rest_framework.pagination import CursorPagination
from rest_framework.response import Response


class LinkHeaderPagination(CursorPagination):
    """ Inform the user of pagination links via response headers, similar to
    what's described in
    https://developer.github.com/guides/traversing-with-pagination/.
    """

    def get_paginated_response(self, data):
        next_url = self.get_next_link()
        previous_url = self.get_previous_link()

        links = []
        for url, label in (
            (previous_url, "prev"),
            (next_url, "next"),
        ):
            if url is not None:
                links.append('<{}>; rel="{}"; results="true"'.format(url, label))
            else:
                links.append('<>; rel="{}"; results="false"'.format(label))

        headers = {"Link": ", ".join(links)} if links else {}

        return Response(data, headers=headers)
