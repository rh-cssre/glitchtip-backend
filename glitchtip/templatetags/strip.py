import re

from django import template
from django.template.defaultfilters import stringfilter

register = template.Library()


@register.filter()
@stringfilter
def stripurlchars(string):
    stripped_text = re.sub(r"\.com|http|\/|\.|\:|\$", "", string)
    return stripped_text[:60]
