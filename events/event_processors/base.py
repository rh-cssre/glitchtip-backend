from abc import ABC, abstractmethod


class EventProcessorBase(ABC):
    @abstractmethod
    def should_run(self) -> bool:
        return False

    @abstractmethod
    def transform(self):
        return self.data

    def __init__(self, project, release, data):
        self.project = project
        self.release = release
        self.data = data

    def run(self):
        if self.should_run():
            self.transform()
