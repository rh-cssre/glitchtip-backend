from typing import Any

from django.http import Http404, HttpResponse
from ninja import ModelSchema, Router
from ninja.errors import ValidationError
from pydantic import ValidationInfo, field_validator

from bitfield.types import BitHandler
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import apaginate

from .models import APIToken

router = Router()


def bitfield_to_internal_value(model, v: Any, info: ValidationInfo) -> int:
    if not info.field_name:
        raise Exception("Improperly used bitfield_to_internal_value")

    if isinstance(v, list):
        model_field = getattr(model, info.field_name)
        result = BitHandler(0, model_field.keys())
        for k in v:
            try:
                setattr(result, str(k), True)
            except AttributeError:
                raise ValidationError([{"scopes": "Invalid scope"}])
        v = result

    if isinstance(v, BitHandler):
        return v.mask

    return v


class APITokenIn(ModelSchema):
    scopes: int

    class Meta:
        model = APIToken
        fields = ("label",)
        fields_optional = ["label"]

    @field_validator("scopes", mode="before")
    @classmethod
    def scopes_to_bitfield(cls, v, info: ValidationInfo):
        return bitfield_to_internal_value(cls.Meta.model, v, info)


class APITokenSchema(ModelSchema):
    scopes: list[str]

    class Meta:
        model = APIToken
        fields = ("label", "created", "token", "id")

    @staticmethod
    def resolve_scopes(obj) -> list[str]:
        """Example: ['member:read']"""
        return [i[0] for i in obj.scopes.items() if i[1] is True]


def get_queryset(request: AuthHttpRequest):
    return APIToken.objects.filter(user_id=request.auth)


@router.get("api-tokens/", response=list[APITokenSchema])
@apaginate
async def list_api_tokens(request: AuthHttpRequest, response: HttpResponse):
    return get_queryset(request)


@router.post("api-tokens/", response={201: APITokenSchema})
async def create_api_token(request: AuthHttpRequest, payload: APITokenIn):
    return await APIToken.objects.acreate(
        **payload.dict(exclude_none=True), user_id=request.auth
    )


@router.delete("api-tokens/{token_id}/", response={204: None})
async def delete_api_token(request: AuthHttpRequest, token_id: int):
    result = await get_queryset(request).filter(id=token_id).adelete()
    if not result[0]:
        raise Http404()
    return
