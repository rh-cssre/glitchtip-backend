from rest_framework import views
from rest_framework.response import Response

from .serializers import ImportSerializer
from .importer import GlitchTipImporter


class ImportAPIView(views.APIView):
    serializer_class = ImportSerializer

    def post(self, request):
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        importer = GlitchTipImporter(
            data["url"], data["authToken"], data["organizationSlug"]
        )
        importer.check_auth()
        importer.run(organization_id=data["organizationSlug"].pk)

        return Response()
