from base64 import b64decode, b64encode
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib import parse

from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext as _
from ninja import Field, Schema
from ninja.pagination import PaginationBase
from pydantic import field_validator

# Code in this file is taken directly from https://github.com/vitalik/django-ninja/pull/836
# and should be swapped out once that MR is merged.

@dataclass
class Cursor:
    offset: int = 0
    reverse: bool = False
    position: Optional[str] = None


def _clamp(val: int, min_: int, max_: int) -> int:
    return max(min_, min(val, max_))


def _reverse_order(order: tuple) -> tuple:
    """
    Reverse the ordering specification for a Django ORM query.

    Given an order_by tuple such as `('-created', 'uuid')` reverse the
    ordering and return a new tuple, eg. `('created', '-uuid')`.
    """

    def invert(x: str) -> str:
        return x[1:] if x.startswith("-") else f"-{x}"

    return tuple(invert(item) for item in order)


def _replace_query_param(url: str, key: str, val: str) -> str:
    scheme, netloc, path, query, fragment = parse.urlsplit(url)
    query_dict = parse.parse_qs(query, keep_blank_values=True)
    query_dict[key] = [val]
    query = parse.urlencode(sorted(query_dict.items()), doseq=True)
    return parse.urlunsplit((scheme, netloc, path, query, fragment))


class CursorPagination(PaginationBase):
    class Input(Schema):
        limit: Optional[int] = Field(
            None, description=_("Number of results to return per page.")
        )
        cursor: Optional[str] = Field(
            None, description=_("The pagination cursor value."), validate_default=True
        )

        @field_validator("cursor")
        @classmethod
        def decode_cursor(cls, encoded_cursor: Optional[str]) -> Cursor:
            if encoded_cursor is None:
                return Cursor()

            try:
                querystring = b64decode(encoded_cursor).decode()
                tokens = parse.parse_qs(querystring, keep_blank_values=True)

                offset = int(tokens.get("o", ["0"])[0])
                offset = _clamp(offset, 0, CursorPagination._offset_cutoff)

                reverse = tokens.get("r", ["0"])[0]
                reverse = bool(int(reverse))

                position = tokens.get("p", [None])[0]
            except (TypeError, ValueError) as e:
                raise ValueError(_("Invalid cursor.")) from e

            return Cursor(offset=offset, reverse=reverse, position=position)

    class Output(Schema):
        results: List[Any] = Field(description=_("The page of objects."))
        count: int = Field(
            description=_("The total number of results across all pages.")
        )
        next: Optional[str] = Field(
            description=_("URL of next page of results if there is one.")
        )
        previous: Optional[str] = Field(
            description=_("URL of previous page of results if there is one.")
        )

    items_attribute = "results"
    default_ordering = ("-created",)
    max_page_size = 100
    _offset_cutoff = 100  # limit to protect against possibly malicious queries

    def paginate_queryset(
        self, queryset: QuerySet, pagination: Input, request: HttpRequest, **params
    ) -> dict:
        limit = _clamp(pagination.limit or self.max_page_size, 0, self.max_page_size)

        if not queryset.query.order_by:
            queryset = queryset.order_by(*self.default_ordering)

        order = queryset.query.order_by
        total_count = queryset.count()

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

        # If we have an offset cursor then offset the entire page by that amount.
        # We also always fetch an extra item in order to determine if there is a
        # page following on from this one.
        results = list(queryset[cursor.offset : cursor.offset + limit + 1])
        page = list(results[:limit])

        # Determine the position of the final item following the page.
        if len(results) > len(page):
            has_following_position = True
            following_position = self._get_position_from_instance(results[-1], order)
        else:
            has_following_position = False
            following_position = None

        if cursor.reverse:
            # If we have a reverse queryset, then the query ordering was in reverse
            # so we need to reverse the items again before returning them to the user.
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

        return {
            "results": page,
            "count": total_count,
            "next": self.next_link(
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
            else None,
            "previous": self.previous_link(
                base_url,
                page,
                cursor,
                order,
                has_next,
                limit,
                next_position,
                previous_position,
            )
            if has_previous
            else None,
        }

    def _encode_cursor(self, cursor: Cursor, base_url: str) -> str:
        tokens = {}
        if cursor.offset != 0:
            tokens["o"] = str(cursor.offset)
        if cursor.reverse:
            tokens["r"] = "1"
        if cursor.position is not None:
            tokens["p"] = cursor.position

        querystring = parse.urlencode(tokens, doseq=True)
        encoded = b64encode(querystring.encode()).decode()
        return _replace_query_param(base_url, "cursor", encoded)

    def next_link(
        self,
        base_url: str,
        page: list,
        cursor: Cursor,
        order: tuple,
        has_previous: bool,
        limit: int,
        next_position: str,
        previous_position: str,
    ) -> str:
        if page and cursor.reverse and cursor.offset:
            # If we're reversing direction and we have an offset cursor
            # then we cannot use the first position we find as a marker.
            compare = self._get_position_from_instance(page[-1], order)
        else:
            compare = next_position
        offset = 0

        has_item_with_unique_position = False
        for item in reversed(page):
            position = self._get_position_from_instance(item, order)
            if position != compare:
                # The item in this position and the item following it
                # have different positions. We can use this position as
                # our marker.
                has_item_with_unique_position = True
                break

            # The item in this position has the same position as the item
            # following it, we can't use it as a marker position, so increment
            # the offset and keep seeking to the previous item.
            compare = position
            offset += 1

        if page and not has_item_with_unique_position:
            # There were no unique positions in the page.
            if not has_previous:
                # We are on the first page.
                # Our cursor will have an offset equal to the page size,
                # but no position to filter against yet.
                offset = limit
                position = None
            elif cursor.reverse:
                # The change in direction will introduce a paging artifact,
                # where we end up skipping forward a few extra items.
                offset = 0
                position = previous_position
            else:
                # Use the position from the existing cursor and increment
                # it's offset by the page size.
                offset = cursor.offset + limit
                position = previous_position

        if not page:
            position = next_position

        next_cursor = Cursor(offset=offset, reverse=False, position=position)
        return self._encode_cursor(next_cursor, base_url)

    def previous_link(
        self,
        base_url: str,
        page: list,
        cursor: Cursor,
        order: tuple,
        has_next: bool,
        limit: int,
        next_position: str,
        previous_position: str,
    ):
        if page and not cursor.reverse and cursor.offset:
            # If we're reversing direction and we have an offset cursor
            # then we cannot use the first position we find as a marker.
            compare = self._get_position_from_instance(page[0], order)
        else:
            compare = previous_position
        offset = 0

        has_item_with_unique_position = False
        for item in page:
            position = self._get_position_from_instance(item, order)
            if position != compare:
                # The item in this position and the item following it
                # have different positions. We can use this position as
                # our marker.
                has_item_with_unique_position = True
                break

            # The item in this position has the same position as the item
            # following it, we can't use it as a marker position, so increment
            # the offset and keep seeking to the previous item.
            compare = position
            offset += 1

        if page and not has_item_with_unique_position:
            # There were no unique positions in the page.
            if not has_next:
                # We are on the final page.
                # Our cursor will have an offset equal to the page size,
                # but no position to filter against yet.
                offset = limit
                position = None
            elif cursor.reverse:
                # Use the position from the existing cursor and increment
                # it's offset by the page size.
                offset = cursor.offset + limit
                position = next_position
            else:
                # The change in direction will introduce a paging artifact,
                # where we end up skipping back a few extra items.
                offset = 0
                position = next_position

        if not page:
            position = previous_position

        cursor = Cursor(offset=offset, reverse=True, position=position)
        return self._encode_cursor(cursor, base_url)

    def _get_position_from_instance(self, instance, ordering) -> str:
        field_name = ordering[0].lstrip("-")
        if isinstance(instance, dict):
            attr = instance[field_name]
        else:
            attr = getattr(instance, field_name)
        return str(attr)
