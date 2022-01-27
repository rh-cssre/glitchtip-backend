from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.microsoft.views import MicrosoftGraphOAuth2Adapter
from allauth.socialaccount.providers.gitea.views import GiteaOAuth2Adapter

SOCIAL_ADAPTER_MAP = {
    "github": GitHubOAuth2Adapter,
    "gitlab": GitLabOAuth2Adapter,
    "google": GoogleOAuth2Adapter,
    "microsoft": MicrosoftGraphOAuth2Adapter,
    "gitea": GiteaOAuth2Adapter,
}
