from rest_framework import exceptions, serializers

from users.serializers import UserSerializer

from .models import Comment


class CommentDataSerializer(serializers.Field):
    def to_internal_value(self, data):
        try:
            text = data["text"]
        except Exception:
            raise exceptions.ValidationError(
                "Comment text should be sent as nested dictionary."
            )
        return text

    def to_representation(self, value):
        return {"text": value}


class CommentSerializer(serializers.ModelSerializer):
    data = CommentDataSerializer(source="text")
    type = serializers.CharField(default="note", read_only=True)
    dateCreated = serializers.DateTimeField(source="created", read_only=True)
    user = UserSerializer(required=False, read_only=True)

    class Meta:
        model = Comment
        fields = (
            "data",
            "type",
            "dateCreated",
            "user",
        )
