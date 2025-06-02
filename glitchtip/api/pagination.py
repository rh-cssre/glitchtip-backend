from typing import TYPE_CHECKING
from urllib import parse

from django.http import HttpRequest, HttpResponse
from ninja.conf import settings as ninja_settings
from ninja_cursor_pagination import CursorPagination, _clamp, _reverse_order

if TYPE_CHECKING:
    from django.db.models import QuerySet


class AsyncLinkHeaderPagination(CursorPagination):
    max_hits = 1000

    # Remove Output schema because we only want to return a list of items
    Output = None

    async def get_results(self, queryset: "QuerySet", cursor, limit):
        return [
            obj async for obj in queryset[cursor.offset : cursor.offset + limit + 1]
        ]

    async def apaginate_queryset(
        self,
        queryset: "QuerySet",
        pagination: CursorPagination.Input,
        request: HttpRequest,
        response: HttpResponse,
        **params,
    ) -> dict:
        limit = _clamp(
            pagination.limit or ninja_settings.PAGINATION_PER_PAGE,
            0,
            self.max_page_size,
        )

        full_queryset = queryset
        if not queryset.query.order_by:
            queryset = queryset.order_by(*self.default_ordering)

        order = queryset.query.order_by

        base_url = request.build_absolute_uri()
        cursor = pagination.cursor

        if cursor.reverse:
            queryset = queryset.order_by(*_reverse_order(order))

        if cursor.position is not None:
            is_reversed = order[0].startswith("-")
            order_attr = order[0].lstrip("-")

            if cursor.reverse != is_reversed:
                queryset = queryset.filter(**{f"{order_attr}__lt": cursor.position})
            else:
                queryset = queryset.filter(**{f"{order_attr}__gt": cursor.position})

        results = await self.get_results(queryset, cursor, limit)
        page = list(results[:limit])

        if len(results) > len(page):
            has_following_position = True
            following_position = self._get_position_from_instance(results[-1], order)
        else:
            has_following_position = False
            following_position = None

        if cursor.reverse:
            page = list(reversed(page))

            has_next = (cursor.position is not None) or (cursor.offset > 0)
            has_previous = has_following_position
            next_position = cursor.position if has_next else None
            previous_position = following_position if has_previous else None
        else:
            has_next = has_following_position
            has_previous = (cursor.position is not None) or (cursor.offset > 0)
            next_position = following_position if has_next else None
            previous_position = cursor.position if has_previous else None

        next = (
            self.next_link(
                base_url=base_url,
                page=page,
                cursor=cursor,
                order=order,
                has_previous=has_previous,
                limit=limit,
                next_position=next_position,
                previous_position=previous_position,
            )
            if has_next
            else None
        )

        previous = (
            self.previous_link(
                base_url=base_url,
                page=page,
                cursor=cursor,
                order=order,
                has_next=has_next,
                limit=limit,
                next_position=next_position,
                previous_position=previous_position,
            )
            if has_previous
            else None
        )

        total_count = 0
        if has_next or has_previous:
            total_count = await self._aitems_count(full_queryset)
        else:
            total_count = len(page)

        links = []
        for url, label in (
            (previous, "previous"),
            (next, "next"),
        ):
            if url is not None:
                parsed = parse.urlparse(url)
                cursor = parse.parse_qs(parsed.query).get("cursor", [""])[0]
                links.append(
                    '<{}>; rel="{}"; results="true"; cursor="{}"'.format(
                        url, label, cursor
                    )
                )
            else:
                links.append('<{}>; rel="{}"; results="false"'.format(base_url, label))

        response["Link"] = {", ".join(links)} if links else {}
        response["X-Max-Hits"] = self.max_hits
        response["X-Hits"] = total_count

        return page

    async def _aitems_count(self, queryset: "QuerySet") -> int:
        return await queryset.order_by()[: self.max_hits].acount()  # type: ignore
