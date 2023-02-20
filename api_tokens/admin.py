from bitfield import BitField
from bitfield.forms import BitFieldCheckboxSelectMultiple
from django.contrib import admin

from .models import APIToken


class APITokenAdmin(admin.ModelAdmin):
    raw_id_fields = ("user",)
    list_display = ("token", "user", "label")
    readonly_fields = ("token",)
    formfield_overrides = {
        BitField: {"widget": BitFieldCheckboxSelectMultiple},
    }


admin.site.register(APIToken, APITokenAdmin)
