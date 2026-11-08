"""Shared test doubles."""


class FakeOutput:
    def __init__(self, name):
        self.name = name
        self.frames = []
        self.tuning = None

    def show(self, frame):
        self.frames.append(frame)

    def release(self):
        pass

    def close(self):
        pass

    def set_tuning(self, brightness, correction):
        self.tuning = (brightness, correction)
