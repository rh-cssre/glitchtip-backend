from collections import OrderedDict
from typing import List
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


class ForgivingHStoreField(serializers.HStoreField):
    def run_child_validation(self, data):
        result = {}
        errors = OrderedDict()

        for key, value in data.items():
            key = str(key)

            try:
                result[key] = self.child.run_validation(value)
            except ValidationError as e:
                details: List[ErrorValueDetail] = []
                for detail in e.detail:
                    details.append(ErrorValueDetail(str(detail), detail.code, value))
                errors[key] = details

        if errors:
            handled_errors = self.context.get("handled_errors", {})
            self.context["handled_errors"] = handled_errors | {self.field_name: errors}
        return result
