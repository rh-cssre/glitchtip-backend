import inspect
from functools import partial, wraps
from typing import Any, Callable, Type
from urllib import parse

from asgiref.sync import sync_to_async
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from ninja.conf import settings as ninja_settings
from ninja.constants import NOT_SET
from ninja.pagination import PaginationBase, make_response_paginated
from ninja.utils import contribute_operation_args, contribute_operation_callback

from .cursor_pagination import CursorPagination, _clamp, _reverse_order


class AsyncLinkHeaderPagination(CursorPagination):
    max_hits = 1000

    # Remove Output schema because we only want to return a list of items
    Output = None

    async def paginate_queryset(
        self, queryset: QuerySet, pagination: CursorPagination.Input, request: HttpRequest, response: HttpResponse, **params
    ) -> dict:
        limit = _clamp(pagination.limit or ninja_settings.PAGINATION_PER_PAGE, 0, self.max_page_size)

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

        @sync_to_async
        def get_results():
            return list(queryset[cursor.offset : cursor.offset + limit + 1])

        results = await get_results()
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

        next = self.next_link(
                    base_url,
                    page,
                    cursor,
                    order,
                    has_previous,
                    limit,
                    next_position,
                    previous_position,
                ) if has_next else None

        previous = self.previous_link(
                    base_url,
                    page,
                    cursor,
                    order,
                    has_next,
                    limit,
                    next_position,
                    previous_position,
                ) if has_next else None

        total_count = 0
        if has_next or has_previous:
            total_count = await self._items_count(full_queryset)
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
                links.append(
                    '<{}>; rel="{}"; results="false"'.format(base_url, label)
                )

        response["Link"] = {", ".join(links)} if links else {}
        response["X-Max-Hits"] = self.max_hits
        response["X-Hits"] = total_count

        return page

    @sync_to_async
    def _items_count(self, queryset: QuerySet) -> int:
        return queryset.order_by()[: self.max_hits].count()  # type: ignore

# async pagination based on https://github.com/vitalik/django-ninja/issues/547#issuecomment-1331292288
def apaginate(func_or_pgn_class: Any = NOT_SET, **paginator_params) -> Callable:
    isfunction = inspect.isfunction(func_or_pgn_class)
    isnotset = func_or_pgn_class == NOT_SET

    pagination_class: Type[PaginationBase] = AsyncLinkHeaderPagination

    if isfunction:
        return _inject_async_pagination(func_or_pgn_class, pagination_class)

    if not isnotset:
        pagination_class = func_or_pgn_class

    def wrapper(func: Callable) -> Any:
        return _inject_async_pagination(func, pagination_class, **paginator_params)

    return wrapper


def _inject_async_pagination(
    func: Callable,
    paginator_class: Type[PaginationBase],
    **paginator_params: Any,
) -> Callable:
    paginator: PaginationBase = paginator_class(**paginator_params)

    @wraps(func)
    async def view_with_pagination(request: HttpRequest, **kwargs: Any) -> Any:
        pagination_params = kwargs.pop("ninja_pagination")
        if paginator.pass_parameter:
            kwargs[paginator.pass_parameter] = pagination_params

        items = await func(request, **kwargs)

        result = await paginator.paginate_queryset(items, pagination=pagination_params, request=request, **kwargs)
        if paginator.Output:
            result[paginator.items_attribute] = list(result[paginator.items_attribute])
        return result

    contribute_operation_args(
        view_with_pagination,
        "ninja_pagination",
        paginator.Input,
        paginator.InputSource,
    )

    if paginator.Output:
        contribute_operation_callback(
            view_with_pagination,
            partial(make_response_paginated, paginator),
        )

    return view_with_pagination
