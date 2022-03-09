from abc import ABC, abstractmethod


class EventProcessorBase(ABC):
    @abstractmethod
    def should_run(self, data) -> bool:
        return False

    @abstractmethod
    def transform(self, data):
        return data

    def __init__(self, project, release):
        self.project = project
        self.release = release

    def run(self, data):
        if self.should_run(data):
            self.transform(data)
