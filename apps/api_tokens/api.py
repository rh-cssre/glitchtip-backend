from django.http import Http404, HttpResponse
from ninja import Router
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest, SessionAuth

from .models import APIToken
from .schema import APITokenIn, APITokenSchema

router = Router(auth=SessionAuth())


def get_queryset(request: AuthHttpRequest):
    user_id = request.auth.user_id
    return APIToken.objects.filter(user_id=user_id)


@router.get("api-tokens/", response=list[APITokenSchema])
@paginate
async def list_api_tokens(request: AuthHttpRequest, response: HttpResponse):
    return get_queryset(request)


@router.post("api-tokens/", response={201: APITokenSchema})
async def create_api_token(request: AuthHttpRequest, payload: APITokenIn):
    user_id = request.auth.user_id
    return await APIToken.objects.acreate(
        **payload.dict(exclude_none=True), user_id=user_id
    )


@router.delete("api-tokens/{token_id}/", response={204: None})
async def delete_api_token(request: AuthHttpRequest, token_id: int):
    result = await get_queryset(request).filter(id=token_id).adelete()
    if not result[0]:
        raise Http404()
    return
