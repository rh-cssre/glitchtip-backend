from abc import ABC, abstractmethod


class EventProcessorBase(ABC):
    @abstractmethod
    def should_run(self) -> bool:
        return False

    @abstractmethod
    def transform(self):
        return self.data

    def __init__(self, project, release_id, data):
        self.project = project
        self.release_id = release_id
        self.data = data

    def run(self):
        if self.should_run():
            self.transform()
