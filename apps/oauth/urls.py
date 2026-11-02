from django.urls import path

from .views import oauth_consent

urlpatterns = [
    path("", oauth_consent, name="oauth_consent"),
]
