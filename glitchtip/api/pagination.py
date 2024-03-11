import inspect
from abc import abstractmethod
from functools import partial, wraps
from typing import Any, AsyncGenerator, Callable, List, Type, Union
from urllib import parse

from asgiref.sync import sync_to_async
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils.module_loading import import_string
from ninja.conf import settings as ninja_settings
from ninja.constants import NOT_SET
from ninja.pagination import PaginationBase, make_response_paginated
from ninja.utils import (
    contribute_operation_args,
    contribute_operation_callback,
    is_async_callable,
)

from .cursor_pagination import CursorPagination, _clamp, _reverse_order


class AsyncPaginationBase(PaginationBase):
    @abstractmethod
    async def apaginate_queryset(
        self,
        queryset: QuerySet,
        pagination: Any,
        **params: Any,
    ) -> Any:
        pass  # pragma: no cover

    async def _aitems_count(self, queryset: QuerySet) -> int:
        try:
            return await queryset.all().acount()
        except AttributeError:
            return len(queryset)


class AsyncLinkHeaderPagination(CursorPagination):
    max_hits = 1000

    # Remove Output schema because we only want to return a list of items
    Output = None

    async def apaginate_queryset(
        self,
        queryset: QuerySet,
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

        next = (
            self.next_link(
                base_url,
                page,
                cursor,
                order,
                has_previous,
                limit,
                next_position,
                previous_position,
            )
            if has_next
            else None
        )

        previous = (
            self.previous_link(
                base_url,
                page,
                cursor,
                order,
                has_next,
                limit,
                next_position,
                previous_position,
            )
            if has_next
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

    async def _aitems_count(self, queryset: QuerySet) -> int:
        return await queryset.order_by()[: self.max_hits].acount()  # type: ignore


def _inject_pagination(
    func: Callable,
    paginator_class: Type[Union[PaginationBase, AsyncPaginationBase]],
    **paginator_params: Any,
) -> Callable:
    paginator = paginator_class(**paginator_params)
    if is_async_callable(func):

        @wraps(func)
        async def view_with_pagination(request: HttpRequest, **kwargs: Any) -> Any:
            pagination_params = kwargs.pop("ninja_pagination")
            if paginator.pass_parameter:
                kwargs[paginator.pass_parameter] = pagination_params

            items = await func(request, **kwargs)

            result = await paginator.apaginate_queryset(
                items, pagination=pagination_params, request=request, **kwargs
            )

            async def evaluate(results: Union[List, QuerySet]) -> AsyncGenerator:
                for result in results:
                    yield result

            if paginator.Output:  # type: ignore
                result[paginator.items_attribute] = [
                    result
                    async for result in evaluate(result[paginator.items_attribute])
                ]
            return result
    else:

        @wraps(func)
        def view_with_pagination(request: HttpRequest, **kwargs: Any) -> Any:
            pagination_params = kwargs.pop("ninja_pagination")
            if paginator.pass_parameter:
                kwargs[paginator.pass_parameter] = pagination_params

            items = func(request, **kwargs)

            result = paginator.paginate_queryset(
                items, pagination=pagination_params, request=request, **kwargs
            )
            if paginator.Output:  # type: ignore
                result[paginator.items_attribute] = list(
                    result[paginator.items_attribute]
                )
                # ^ forcing queryset evaluation #TODO: check why pydantic did not do it here
            return result

    contribute_operation_args(
        view_with_pagination,
        "ninja_pagination",
        paginator.Input,
        paginator.InputSource,
    )

    if paginator.Output:  # type: ignore
        contribute_operation_callback(
            view_with_pagination,
            partial(make_response_paginated, paginator),
        )

    return view_with_pagination


def paginate(func_or_pgn_class: Any = NOT_SET, **paginator_params: Any) -> Callable:
    """
    @api.get(...
    @paginate
    def my_view(request):

    or

    @api.get(...
    @paginate(PageNumberPagination)
    def my_view(request):

    """

    isfunction = inspect.isfunction(func_or_pgn_class)
    isnotset = func_or_pgn_class == NOT_SET

    pagination_class: Type[Union[PaginationBase, AsyncPaginationBase]] = import_string(
        ninja_settings.PAGINATION_CLASS
    )

    if isfunction:
        return _inject_pagination(func_or_pgn_class, pagination_class)

    if not isnotset:
        pagination_class = func_or_pgn_class

    def wrapper(func: Callable) -> Any:
        return _inject_pagination(func, pagination_class, **paginator_params)

    return wrapper
