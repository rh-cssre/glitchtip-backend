from django.contrib import admin
from .models import Team


class TeamAdmin(admin.ModelAdmin):
    search_fields = ("slug",)
    list_display = ("slug", "organization")
    raw_id_fields = ("organization",)
    filter_horizontal = ("members", "projects")


admin.site.register(Team, TeamAdmin)
