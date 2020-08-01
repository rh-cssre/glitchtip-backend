from typing import Optional


class EventContextsTagProcessor:
    """
    Abstract class for generating tags based on event contexts
    This is done to make some contexts searchable
    """

    def get_tag_values(self, event) -> Optional[str]:
        contexts = event.get("contexts")
        if contexts:
            return self.get_tag_from_contexts(contexts)

    def get_tag_from_contexts(self, contexts):
        raise NotImplementedError()


class BrowserNameTagProcessor(EventContextsTagProcessor):
    """
    Adds browser.name tag from contexts
    """

    tag = "browser.name"

    def get_tag_from_contexts(self, contexts) -> Optional[str]:
        return contexts.get("browser", {}).get("name")


class BrowserTagProcessor(EventContextsTagProcessor):
    """
    Adds browser tag from request, which user_agents refers to as family + version_string
    """

    tag = "browser"

    def get_tag_from_contexts(self, contexts) -> Optional[str]:
        browser = contexts.get("browser")
        if browser:
            name = browser.get("name")
            if name:
                version = browser.get("version")
                if version:
                    return name + " " + version
                return name


class OsTagProcessor(EventContextsTagProcessor):
    """
    Adds os.name tag from request, which user_agents refers to as os family
    """

    tag = "os.name"

    def get_tag_from_contexts(self, contexts) -> Optional[str]:
        return contexts.get("os", {}).get("name")


class DeviceTagProcessor(EventContextsTagProcessor):
    """
    Adds device tag from request, which user_agents refers to as device model
    This field is fairly unreliable as browsers such as Firefox do not share it for privacy reasons
    """

    tag = "device"

    def get_tag_from_contexts(self, contexts) -> Optional[str]:
        return contexts.get("device", {}).get("model")


TAG_PROCESSORS = [
    BrowserTagProcessor,
    BrowserNameTagProcessor,
    OsTagProcessor,
    DeviceTagProcessor,
]
