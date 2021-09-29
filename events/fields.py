from collections import OrderedDict
from urllib.parse import parse_qs
from typing import List
import re
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, ErrorDetail


class ErrorValueDetail(ErrorDetail):
    """Extended ErrorDetail with validation value"""

    value = None

    def __new__(cls, string, code=None, value=None):
        self = super().__new__(cls, string, code)
        self.value = value
        return self

    def __repr__(self):
        return "ErrorDetail(string=%r, code=%r, value=%r)" % (
            str(self),
            self.code,
            self.value,
        )


class GenericField(serializers.Field):
    def to_internal_value(self, data):
        return data


class ForgivingFieldMixin:
    def update_handled_errors_context(self, errors: List[ErrorValueDetail]):
        if errors:
            handled_errors = self.context.get("handled_errors", {})
            self.context["handled_errors"] = handled_errors | {self.field_name: errors}


class ForgivingHStoreField(ForgivingFieldMixin, serializers.HStoreField):
    def run_child_validation(self, data):
        result = {}
        errors: List[ErrorValueDetail] = []

        for key, value in data.items():
            if value is None:
                continue
            key = str(key)

            try:
                result[key] = self.child.run_validation(value)
            except ValidationError as e:
                for detail in e.detail:
                    errors.append(ErrorValueDetail(str(detail), detail.code, value))

        if errors:
            self.update_handled_errors_context(errors)
        return result


class ForgivingDisallowRegexField(ForgivingFieldMixin, serializers.CharField):
    """ Disallow bad matches, set disallow_regex kwarg to use """

    def __init__(self, **kwargs):
        self.disallow_regex = kwargs.pop("disallow_regex", None)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        if self.disallow_regex:
            pattern = re.compile(self.disallow_regex)
            if pattern.match(data) is None:
                error = ErrorValueDetail(
                    "invalid characters in string", "invalid_data", data
                )
                self.update_handled_errors_context([error])
                return None
        return data


class QueryStringField(serializers.ListField):
    """
    Can be given as unparsed string, dictionary, or list of tuples
    Should store as List[List[str]] where inner List is always of length 2
    """

    child = serializers.ListField(child=serializers.CharField())

    def to_internal_value(self, data):
        if isinstance(data, str) and data:
            qs = parse_qs(data)
            result = []
            for key, values in qs.items():
                for value in values:
                    result.append([key, value])
            return result
        elif isinstance(data, dict):
            return [[key, value] for key, value in data.items()]
        elif isinstance(data, list):
            result = []
            for item in data:
                if isinstance(item, list) and len(item) >= 2:
                    result.append(item[:2])
            return result
        return None
