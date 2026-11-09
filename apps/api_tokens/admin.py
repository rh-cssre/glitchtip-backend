from django.contrib import admin

from bitfield import BitField
from bitfield.forms import BitFieldCheckboxSelectMultiple

from .models import APIToken


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    raw_id_fields = ("user",)
    list_display = ("token", "user", "label")
    readonly_fields = ("token",)
    formfield_overrides = {
        BitField: {"widget": BitFieldCheckboxSelectMultiple},
    }
