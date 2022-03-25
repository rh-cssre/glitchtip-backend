from .base import EventProcessorBase


class JavascriptEventProcessor(EventProcessorBase):
    def should_run(self, data):
        return data.get("platform") in ("javascript", "node") and self.release

    def transform(self, data):
        release_files = self.release.releasefile_set.all()
        return data
