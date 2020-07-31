""" Roughly a replacement of sentry/plugins/sentry_useragents """
import abc
from typing import Optional
from user_agents import parse


class UserAgentProcessor(abc.ABC):
    """ Abstract class for processing user agent related tags """

    def get_tag_values(self, event) -> Optional[str]:
        headers = event.get("request", {}).get("headers")
        if not headers:
            return
        ua_string = next(x[1] for x in headers if x[0] == "User-Agent")
        ua = parse(ua_string)
        tag_value = self.get_tag_from_ua(ua)
        if tag_value:
            return tag_value.strip()

    @abc.abstractmethod
    def get_tag_from_ua(self, ua):
        raise NotImplementedError()


class BrowserNameProcessor(UserAgentProcessor):
    """
    Adds browser.name tag from request, which user_agents refers to as family
    """

    tag = "browser.name"

    def get_tag_from_ua(self, ua):
        return ua.browser.family


class BrowserProcessor(UserAgentProcessor):
    """
    Adds browser tag from request, which user_agents refers to as family + version_string
    """

    tag = "browser"

    def get_tag_from_ua(self, ua):
        return ua.browser.family + " " + ua.browser.version_string


class OsProcessor(UserAgentProcessor):
    """
    Adds os.name tag from request, which user_agents refers to as os family
    """

    tag = "os.name"

    def get_tag_from_ua(self, ua):
        return ua.os.family


class DeviceProcessor(UserAgentProcessor):
    """
    Adds device tag from request, which user_agents refers to as device model
    This field is fairly unreliable as browsers such as Firefox do not share it for privacy reasons
    """

    tag = "device"

    def get_tag_from_ua(self, ua):
        return ua.device.model


TAG_PROCESSORS = [BrowserProcessor, BrowserNameProcessor, OsProcessor, DeviceProcessor]
