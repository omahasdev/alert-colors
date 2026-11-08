import threading
from dataclasses import dataclass, field

from alert_colors.domain import Color
from alert_colors.effects import Solid
from alert_colors.frame import Frame
from alert_colors.outputs import BackendWorker

RED, BLUE = Color(255, 0, 0), Color(0, 0, 255)


def frame_of(color):
    return Frame(Solid(color), 0.0)
WAIT_S = 2.0
LONG_RETRY_S = 60.0
SHORT_RETRY_S = 0.05


@dataclass
class RecordingBackend:
    name: str = "rec"
    calls: list = field(default_factory=list)
    fail_times: int = 0
    gate: threading.Event | None = None
    called: threading.Event = field(default_factory=threading.Event)

    def show(self, frame):
        color = frame.color()
        if self.gate:
            self.gate.wait(WAIT_S)
        if self.fail_times:
            self.fail_times -= 1
            self.calls.append(("fail", color))
            self.called.set()
            raise OSError("device gone")
        self.calls.append(("show", color))
        self.called.set()

    def release(self):
        self.calls.append(("release", None))
        self.called.set()

    def describe(self):
        return []


def wait_for(predicate):
    deadline = threading.Event()
    for _ in range(int(WAIT_S / SHORT_RETRY_S)):
        if predicate():
            return True
        deadline.wait(SHORT_RETRY_S)
    return predicate()


def test_slow_backend_does_not_block_caller_and_gets_newest_color():
    gate = threading.Event()
    backend = RecordingBackend(gate=gate)
    worker = BackendWorker(backend, retry_s=LONG_RETRY_S, close_timeout_s=WAIT_S)
    worker.show(frame_of(RED))  # the backend blocks on the gate, but this call returns at once
    worker.show(frame_of(BLUE))
    worker.show(frame_of(RED))
    worker.show(frame_of(BLUE))  # only the newest pending command survives
    gate.set()
    assert wait_for(lambda: ("show", BLUE) in backend.calls)
    worker.close()
    shows = [c for c in backend.calls if c[0] == "show"]
    assert shows[-1] == ("show", BLUE) and len(shows) <= 2
    assert backend.calls[-1] == ("release", None)


def test_failure_releases_backs_off_then_recovers():
    backend = RecordingBackend(fail_times=1)
    worker = BackendWorker(backend, retry_s=SHORT_RETRY_S, close_timeout_s=WAIT_S)
    worker.show(frame_of(RED))
    assert wait_for(lambda: ("release", None) in backend.calls)
    worker.show(frame_of(BLUE))
    assert wait_for(lambda: ("show", BLUE) in backend.calls)
    worker.close()
    assert backend.calls[:2] == [("fail", RED), ("release", None)]


def test_close_interrupts_backoff():
    backend = RecordingBackend(fail_times=1)
    worker = BackendWorker(backend, retry_s=LONG_RETRY_S, close_timeout_s=WAIT_S)
    worker.show(frame_of(RED))
    assert wait_for(lambda: ("release", None) in backend.calls)
    worker.close()
    assert not worker._thread.is_alive()


def test_color_correction_then_brightness():
    from alert_colors.domain import Color

    backend = RecordingBackend()
    worker = BackendWorker(backend, retry_s=LONG_RETRY_S, close_timeout_s=WAIT_S,
                           brightness=0.5, correction=(1.0, 0.5, 0.0))
    worker.show(frame_of(Color(200, 200, 200)))
    assert wait_for(lambda: backend.calls)
    worker.close()
    assert backend.calls[0] == ("show", Color(100, 50, 0))
