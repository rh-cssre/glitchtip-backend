from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Issue, Event
from .serializers import IssueSerializer, StoreSerializer


class IssueViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Issue.objects.all()
    serializer_class = IssueSerializer


class EventStoreAPIView(APIView):
    def nothing(self, metadata):
        ty = metadata.get("type")
        if ty is None:
            return metadata.get("function") or "<unknown>"
        if not metadata.get("value"):
            return ty
        return u"{}: {}".format(
            ty, truncatechars(metadata["value"].splitlines()[0], 100)
        )

    def post(self, request, id: int, format=None):
        serializer = StoreSerializer(data=request.data)
        if serializer.is_valid():
            data = serializer.data
            title = data
            issue = Issue.objects.get_or_create()
            event = Event.objects.create(
                event_id=data["event_id"],
                exception=data["exception"],
                level=data["level"],
                platform=data["platform"],
                sdk=data["sdk"],
                release=data["release"],
                breadcrumbs=data["breadcrumbs"],
                request=data["request"],
                issue=issue,
            )
            return Response(status=status.HTTP_200_OK)
        return Response()
