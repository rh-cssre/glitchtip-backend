from django import template
from django.conf import settings
from django.urls import reverse

register = template.Library()

@register.simple_tag()
def get_domain():
    return settings.GLITCHTIP_DOMAIN.geturl()
