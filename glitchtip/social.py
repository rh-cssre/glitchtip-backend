from rest_auth.registration.views import SocialConnectView, SocialLoginView
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.microsoft.views import MicrosoftGraphOAuth2Adapter
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client


class GitlabConnect(SocialConnectView):
    adapter_class = GitLabOAuth2Adapter


class GitlabLogin(SocialLoginView):
    adapter_class = GitLabOAuth2Adapter


class GithubConnect(SocialConnectView):
    adapter_class = GitHubOAuth2Adapter
    client_class = OAuth2Client
    callback_url = "http://localhost:4200/login/github"


class GithubLogin(SocialLoginView):
    adapter_class = GitHubOAuth2Adapter
    client_class = OAuth2Client
    callback_url = "http://localhost:4200/login/github"


class GoogleConnect(SocialConnectView):
    adapter_class = GoogleOAuth2Adapter


class GoogleLogin(SocialLoginView):
    adapter_class = GoogleOAuth2Adapter


class MicrosoftConnect(SocialConnectView):
    adapter_class = MicrosoftGraphOAuth2Adapter


class MicrosoftLogin(SocialLoginView):
    adapter_class = MicrosoftGraphOAuth2Adapter
