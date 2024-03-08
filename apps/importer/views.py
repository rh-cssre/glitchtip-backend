from rest_framework import views
from rest_framework.response import Response

from .importer import GlitchTipImporter
from .serializers import ImportSerializer


class ImportAPIView(views.APIView):
    """Import members, projects, and teams for an organization of which you are an Admin of"""

    serializer_class = ImportSerializer

    def post(self, request):
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        importer = GlitchTipImporter(
            data["url"], data["authToken"], data["organizationSlug"].slug
        )
        importer.check_auth()
        importer.run(organization_id=data["organizationSlug"].pk)

        return Response()
