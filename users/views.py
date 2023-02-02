from allauth.account.models import EmailAddress
from dj_rest_auth.registration.views import (
    SocialAccountDisconnectView as BaseSocialAccountDisconnectView,
)
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_yasg.utils import swagger_auto_schema
from rest_framework import exceptions, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from organizations_ext.models import Organization
from projects.models import UserProjectAlert
from users.utils import is_user_registration_open

from .models import User
from .serializers import (
    ConfirmEmailAddressSerializer,
    EmailAddressSerializer,
    UserNotificationsSerializer,
    UserSerializer,
)


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(
                organizations_ext_organization__slug=organization_slug,
                organizations_ext_organization__users=self.request.user,
            )
        else:
            queryset = queryset.filter(id=self.request.user.id)
        return queryset

    def get_object(self):
        if self.kwargs.get("pk") == "me":
            return self.request.user
        return super().get_object()

    def perform_create(self, serializer):
        organization_slug = self.kwargs.get("organization_slug")
        try:
            Organization.objects.get(slug=organization_slug)
        except Organization.DoesNotExist as err:
            raise exceptions.ValidationError("Organization does not exist") from err
        # TODO deal with organization and users who aren't set up yet
        if not is_user_registration_open() and not self.request.user.is_superuser:
            raise exceptions.PermissionDenied("Registration is not open")
        user = serializer.save()
        return user

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.organizations_ext_organizationuser.filter(
            organizationowner__isnull=False
        ):
            return Response(
                data={
                    "message": "User is organization owner. Delete organization or transfer ownership first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post", "put"])
    def notifications(self, request, pk=None):
        user = self.get_object()

        if request.method == "GET":
            serializer = UserNotificationsSerializer(user)
            return Response(serializer.data)

        serializer = UserNotificationsSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True, methods=["get", "post", "put"], url_path="notifications/alerts"
    )
    def alerts(self, request, pk=None):
        """
        Returns dictionary of project_id: status. Now project_id status means it's "default"

        To update, submit `{project_id: status}` where status is -1 (default), 0, or 1
        """
        user = self.get_object()
        alerts = user.userprojectalert_set.all()

        if request.method == "GET":
            data = {}
            for alert in alerts:
                data[alert.project_id] = alert.status
            return Response(data)

        data = request.data
        try:
            items = [x for x in data.items()]
        except AttributeError as err:
            raise exceptions.ValidationError(
                "Invalid alert format, expected dictionary"
            ) from err
        if len(data) != 1:
            raise exceptions.ValidationError("Invalid alert format, expected one value")
        project_id, alert_status = items[0]
        if alert_status not in [1, 0, -1]:
            raise exceptions.ValidationError("Invalid status, must be -1, 0, or 1")
        alert = alerts.filter(project_id=project_id).first()
        if alert and alert_status == -1:
            alert.delete()
        else:
            UserProjectAlert.objects.update_or_create(
                user=user, project_id=project_id, defaults={"status": alert_status}
            )
        return Response(status=204)


class EmailAddressViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    queryset = EmailAddress.objects.all()
    serializer_class = EmailAddressSerializer
    pagination_class = None

    def get_user(self, user_pk):
        if user_pk == "me":
            return self.request.user
        raise exceptions.ValidationError(
            "Can only change primary email address on own account"
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            # queryset just for schema generation metadata
            return EmailAddress.objects.none()

        user = self.get_user(self.kwargs.get("user_pk"))
        queryset = super().get_queryset().filter(user=user)
        return queryset

    def put(self, request, user_pk, format=None):
        """
        Set a new primary email (must be verified) this will also set the email used when a user logs in.
        """
        user = self.get_user(user_pk)
        try:
            email_address = user.emailaddress_set.get(
                email=request.data.get("email"), verified=True
            )
            email_address.set_as_primary()
        except ObjectDoesNotExist as err:
            raise Http404 from err
        serializer = self.serializer_class(
            instance=email_address, data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, user_pk, format=None):
        user = self.get_user(user_pk)
        try:
            email_address = user.emailaddress_set.get(
                email=request.data.get("email"), primary=False
            )
        except ObjectDoesNotExist as err:
            raise Http404 from err
        email_address.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @swagger_auto_schema(responses={204: "No Content"})
    @action(detail=False, methods=["post"])
    def confirm(self, request, user_pk):
        serializer = ConfirmEmailAddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email_address = get_object_or_404(
            self.get_queryset(), email=serializer.validated_data.get("email")
        )
        email_address.send_confirmation(request)
        return Response(status=204)


class SocialAccountDisconnectView(BaseSocialAccountDisconnectView):
    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except ValidationError as e:
            raise exceptions.ValidationError(e.message)
