from rest_framework import exceptions, mixins, viewsets

from issues.models import Issue

from .models import Comment
from .serializers import CommentSerializer


class CommentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Comment.objects.all()
    serializer_class = CommentSerializer

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = super().get_queryset()
        issue_id = self.kwargs.get("issue_pk")
        if issue_id:
            queryset = queryset.filter(issue_id=issue_id)
        return queryset

    def perform_create(self, serializer):
        try:
            issue = Issue.objects.get(
                id=self.kwargs.get("issue_pk"),
            )
        except Issue.DoesNotExist:
            raise exceptions.ValidationError("Issue does not exist")
        project = issue.project
        serializer.save(issue=issue, project=project, user=self.request.user)
