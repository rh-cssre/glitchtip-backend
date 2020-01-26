from rest_auth.registration.views import SocialConnectView
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter


class GitlabConnect(SocialConnectView):
    adapter_class = GitLabOAuth2Adapter
