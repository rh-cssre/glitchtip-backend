from .base import EventProcessorBase


class JavascriptEventProcessor(EventProcessorBase):
    def should_run(self, data):
        return data.get("platform") in ("javascript", "node")

    def transform(self, data):
        import ipdb

        ipdb.set_trace()
        return data
