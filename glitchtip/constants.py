from allauth.socialaccount.providers.digitalocean.views import DigitalOceanOAuth2Adapter
from allauth.socialaccount.providers.gitea.views import GiteaOAuth2Adapter
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.microsoft.views import MicrosoftGraphOAuth2Adapter
from allauth.socialaccount.providers.nextcloud.views import NextCloudAdapter
from allauth.socialaccount.providers.okta.views import OktaOAuth2Adapter
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectAdapter

SOCIAL_ADAPTER_MAP = {
    "digitalocean": DigitalOceanOAuth2Adapter,
    "github": GitHubOAuth2Adapter,
    "gitlab": GitLabOAuth2Adapter,
    "google": GoogleOAuth2Adapter,
    "microsoft": MicrosoftGraphOAuth2Adapter,
    "gitea": GiteaOAuth2Adapter,
    "nextcloud": NextCloudAdapter,
    "openid_connect": OpenIDConnectAdapter,
    "okta": OktaOAuth2Adapter,
}
