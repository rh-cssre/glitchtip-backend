"""
Roughly a replacement of sentry/plugins/sentry_useragents but for contexts instead of tags
"""
from typing import Optional, Dict
from user_agents import parse


class UserAgentContextProcessor:
    """ Abstract class for processing user agent related contexts """

    def get_context(self, event) -> Optional[Dict[str, str]]:
        headers = event.get("request", {}).get("headers", {})
        try:
            ua_string = next(x[1] for x in headers if x[0] == "User-Agent")
        except StopIteration:
            return  # Not found
        if isinstance(ua_string, list) and len(ua_string) > 0:
            ua_string = ua_string[0]
        if not ua_string:
            return
        ua = parse(ua_string)
        return self.get_context_from_ua(ua)

    @property
    def name(self):
        raise NotImplementedError()

    def get_context_from_ua(self, ua):
        raise NotImplementedError()


class BrowserContextProcessor(UserAgentContextProcessor):
    """
    Browser name (aka family) and version
    """

    name = "browser"

    def get_context_from_ua(self, ua):
        context = {"name": ua.browser.family}
        if ua.browser.version:
            context["version"] = ua.browser.version_string
        return context


class OsContextProcessor(UserAgentContextProcessor):
    """
    Operating System name (aka family) and version
    """

    name = "os"

    def get_context_from_ua(self, ua):
        context = {"name": ua.os.family}
        if ua.os.version:
            context["version"] = ua.os.version_string
        return context


class DeviceContextProcessor(UserAgentContextProcessor):
    """
    Device model, brand, family
    This field is fairly unreliable as browsers such as Firefox do not share it
    """

    name = "device"

    def get_context_from_ua(self, ua):
        context = {
            "family": ua.device.family,
        }
        if ua.device.model:
            context["model"] = ua.device.model
        if ua.device.brand:
            context["brand"] = ua.device.brand
        return context


EVENT_CONTEXT_PROCESSORS = [
    BrowserContextProcessor,
    OsContextProcessor,
    DeviceContextProcessor,
]
