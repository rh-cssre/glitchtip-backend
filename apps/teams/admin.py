from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from import_export.admin import ImportExportModelAdmin

from .models import Team
from .resources import TeamResource


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        organization = cleaned_data.get("organization")
        projects = cleaned_data.get("projects")
        if projects.exclude(organization=organization).exists():
            raise ValidationError(
                "All projects must belong to the same organization as the team."
            )
        return cleaned_data


class TeamAdmin(ImportExportModelAdmin):
    form = TeamForm
    search_fields = ("slug",)
    list_display = ("slug", "organization")
    raw_id_fields = ("organization",)
    filter_horizontal = ("members", "projects")
    resource_class = TeamResource


admin.site.register(Team, TeamAdmin)
