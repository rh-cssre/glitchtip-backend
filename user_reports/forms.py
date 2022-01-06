from django.utils.translation import gettext_lazy as _
from django import forms
from .models import UserReport


class UserReportForm(forms.ModelForm):
    name = forms.CharField(
        max_length=128, widget=forms.TextInput(attrs={"placeholder": _("Jane Doe")})
    )
    email = forms.EmailField(
        max_length=75,
        widget=forms.TextInput(
            attrs={"placeholder": _("jane@example.com"), "type": "email"}
        ),
    )
    comments = forms.CharField(
        widget=forms.Textarea(
            attrs={"placeholder": _("I clicked on 'X' and then hit 'Confirm'")}
        )
    )

    class Meta:
        model = UserReport
        fields = ("name", "email", "comments")
