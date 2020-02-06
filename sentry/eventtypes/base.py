class BaseEvent:
    id = None

    def get_metadata(self, data):
        raise NotImplementedError

    def get_title(self, metadata):
        raise NotImplementedError

    def get_location(self, metadata):
        return None

