from rest_framework import serializers
from bitfield.types import BitHandler
from .models import APIToken


class BitFieldSerializer(serializers.Field):
    """
    BitField model field serializer

    Displays as list of true flag

    Semi inspired from django-bitfield bitfield/forms.py
    """

    def to_internal_value(self, data):
        model_field = getattr(self.root.Meta.model, self.source)
        result = BitHandler(0, model_field.keys())
        for k in data:
            try:
                setattr(result, str(k), True)
            except AttributeError:
                raise serializers.ValidationError("Unknown choice: %r" % (k,))
        return result

    def to_representation(self, value):
        return [i[0] for i in value.items() if i[1] is True]


class APITokenAuthScopesSerializer(serializers.ModelSerializer):
    scopes = BitFieldSerializer()

    class Meta:
        model = APIToken
        fields = ("scopes",)


class APITokenSerializer(APITokenAuthScopesSerializer):
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    scopes = BitFieldSerializer()

    class Meta(APITokenAuthScopesSerializer.Meta):
        fields = ("scopes", "dateCreated", "label", "token", "id")
