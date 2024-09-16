from allauth.account.apps import AccountConfig as BaseAccountConfig


class AccountConfig(BaseAccountConfig):
    def ready(self):
        pass
        # Disable annoying checks
