from apps.api_tokens.models import APIToken


async def validate_token(token: str) -> tuple[int, list[str]]:
    """Validate an API token and return (user_id, scope_list).

    Raises ValueError if the token is invalid or the user is inactive.
    """
    try:
        api_token = await APIToken.objects.aget(token=token, user__is_active=True)
    except APIToken.DoesNotExist:
        raise ValueError("Invalid or expired API token")
    return api_token.user_id, api_token.get_scopes()
