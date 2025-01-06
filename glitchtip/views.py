from django.http import HttpResponse


async def health(request):
    return HttpResponse("ok", content_type="text/plain")
