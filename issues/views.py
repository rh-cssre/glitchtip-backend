from django.db import IntegrityError
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Issue, Event
from .serializers import IssueSerializer, StoreSerializer
from .event_store.error import ErrorEvent


class IssueViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Issue.objects.all()
    serializer_class = IssueSerializer


class EventStoreAPIView(APIView):
    def post(self, request, id: int, format=None):
        serializer = StoreSerializer(data=request.data)
        if serializer.is_valid():
            data = serializer.data
            error = ErrorEvent()
            metadata = error.get_metadata(data)
            issue, _ = Issue.objects.get_or_create(
                title=error.get_title(metadata), location=error.get_location(metadata),
            )
            try:
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
            except IntegrityError:
                return Response(
                    {
                        "error": f"An event with the same ID already exists ({data['event_id'].replace('-', '')})"
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            return Response({"id": data["event_id"].replace("-", "")})
        # TODO {"error": "Invalid api key"}, CSP type, valid json but no type at all
        return Response()
