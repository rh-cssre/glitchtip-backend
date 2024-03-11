from django.urls import path, re_path

from .views import SetupWizardSetTokenView, SetupWizardView

urlpatterns = [
    path("wizard/", SetupWizardView.as_view(), name="setup-wizard"),
    re_path(
        r"wizard/(?P<wizard_hash>\w{64})/$",
        SetupWizardView.as_view(),
        name="setup-wizard",
    ),
    path(
        "wizard-set-token/",
        SetupWizardSetTokenView.as_view(),
        name="setup-wizard-set-token",
    ),
]
