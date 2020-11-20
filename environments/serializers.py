from rest_framework import serializers
from .models import Environment, EnvironmentProject


class EnvironmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Environment
        fields = ("id", "name")


class EnvironmentProjectSerializer(serializers.ModelSerializer):
    name = serializers.StringRelatedField(source="environment")
    isHidden = serializers.BooleanField(source="is_hidden")

    class Meta:
        model = EnvironmentProject
        fields = ("id", "name", "isHidden")
