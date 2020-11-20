from django.contrib import admin
from .models import Environment, EnvironmentProject


class EnvironmentAdmin(admin.ModelAdmin):
    pass


admin.site.register(Environment, EnvironmentAdmin)


class EnvironmentProjectAdmin(admin.ModelAdmin):
    pass


admin.site.register(EnvironmentProject, EnvironmentProjectAdmin)
