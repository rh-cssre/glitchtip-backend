import json
import time

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.views.decorators.http import require_http_methods

from apps.api_tokens.models import generate_token

from .provider import AUTH_CODE_LIFETIME, _grant_cache_key

SIGNED_DATA_MAX_AGE = 600  # 10 minutes


@login_required
@require_http_methods(["GET", "POST"])
def oauth_consent(request):
    try:
        data = signing.loads(request.GET.get("data", ""), max_age=SIGNED_DATA_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired):
        return HttpResponseBadRequest("Invalid or expired authorization request.")

    if request.method == "GET":
        return TemplateResponse(
            request,
            "oauth/consent.html",
            {
                "client_id": data.get("client_id", ""),
                "scopes": data.get("scopes") or [],
                "signed_data": request.GET["data"],
            },
        )

    # POST — user approved
    code = generate_token()
    now = int(time.time())
    grant_data = json.dumps(
        {
            "client_id": data["client_id"],
            "user_id": request.user.id,
            "scopes": data.get("scopes") or [],
            "expires_at": now + AUTH_CODE_LIFETIME,
            "code_challenge": data["code_challenge"],
            "redirect_uri": data["redirect_uri"],
            "redirect_uri_provided_explicitly": data[
                "redirect_uri_provided_explicitly"
            ],
            "resource": data.get("resource"),
        }
    )
    cache.set(_grant_cache_key(code), grant_data, AUTH_CODE_LIFETIME)

    redirect_uri = data["redirect_uri"]
    separator = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{separator}code={code}"
    if data.get("state"):
        location += f"&state={data['state']}"
    return HttpResponseRedirect(location)
