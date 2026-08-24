"""A checkpoint-free CHF runner that speaks the production frame codec."""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from struct import pack
from threading import Condition, Event

from nmrpeak_provider.chf_runner_protocol import (
    AttemptCorrelation,
    ChfRunnerFrame,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    ValidateFrame,
    ValidatedFrame,
    decode_chf_runner_frame,
    encode_chf_runner_frame,
)


class FakeRunnerFault(StrEnum):
    WRONG_VALIDATED_CORRELATION = "wrong_validated_correlation"
    WRONG_RESULT_CORRELATION = "wrong_result_correlation"
    MALFORMED_VALIDATION = "malformed_validation"
    REJECT_GENERATION = "reject_generation"
    EOF_DURING_GENERATION = "eof_during_generation"
    TIMEOUT_DURING_GENERATION = "timeout_during_generation"
    BLOCK_GENERATION = "block_generation"
    RETIRE_SEND_UNCERTAIN = "retire_send_uncertain"


class FakeChfRunnerChannel:
    """Serve one READY boot and fixed candidates over a socket-shaped byte seam."""

    def __init__(
        self,
        ready: ReadyFrame,
        *,
        candidates: object = ("CCO", "OCC"),
        rejected_validations: int = 0,
        fault: FakeRunnerFault | None = None,
    ) -> None:
        self.received_frames: list[ChfRunnerFrame] = []
        self.generate_received = Event()
        self._ready = ready
        self._candidates = list(candidates) if type(candidates) is tuple else candidates
        self._rejected_validations = rejected_validations
        self._fault = fault
        self._pending: AttemptCorrelation | None = None
        self._buffer = bytearray(encode_chf_runner_frame(ready))
        self._closed = False
        self._timeout: float | None = None
        self._condition = Condition()

    @property
    def closed(self) -> bool:
        return self._closed

    def settimeout(self, value: float) -> None:
        self._timeout = value

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise OSError("fake runner channel is closed")
        frame = decode_chf_runner_frame(data)
        self.received_frames.append(frame)
        if type(frame) is ValidateFrame:
            self._accept_validate(frame)
            return
        if type(frame) is GenerateFrame:
            self._accept_generate(frame)
            return
        if type(frame) is RetireFrame and frame.boot_generation == self._ready.boot_generation:
            if self._fault is FakeRunnerFault.RETIRE_SEND_UNCERTAIN:
                raise TimeoutError("fake runner RETIRE send outcome is unknown")
            return
        raise OSError("fake runner received a frame outside its session state")

    def recv_into(self, destination: memoryview) -> int:
        with self._condition:
            if not self._buffer and not self._closed:
                if self._fault is FakeRunnerFault.TIMEOUT_DURING_GENERATION:
                    raise TimeoutError("fake runner response timed out")
                self._condition.wait(timeout=self._timeout)
            if not self._buffer:
                if self._closed:
                    return 0
                raise TimeoutError("fake runner response timed out")
            count = min(len(destination), len(self._buffer))
            destination[:count] = self._buffer[:count]
            del self._buffer[:count]
            return count

    def shutdown(self, _how: int) -> None:
        self.close()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _accept_validate(self, frame: ValidateFrame) -> None:
        if self._pending is not None:
            raise OSError("fake runner already has a validated request")
        if self._rejected_validations:
            self._rejected_validations -= 1
            self._queue(RejectedFrame(frame.correlation))
            return
        self._pending = frame.correlation
        if self._fault is FakeRunnerFault.MALFORMED_VALIDATION:
            self._queue_raw(pack(">I", 2) + b"{}")
            return
        correlation = (
            _different_correlation(frame.correlation)
            if self._fault is FakeRunnerFault.WRONG_VALIDATED_CORRELATION
            else frame.correlation
        )
        self._queue(ValidatedFrame(correlation))

    def _accept_generate(self, frame: GenerateFrame) -> None:
        if frame.correlation != self._pending:
            raise OSError("fake runner generation correlation is not pending")
        self.generate_received.set()
        if self._fault in {
            FakeRunnerFault.TIMEOUT_DURING_GENERATION,
            FakeRunnerFault.BLOCK_GENERATION,
        }:
            return
        if self._fault is FakeRunnerFault.EOF_DURING_GENERATION:
            self.close()
            return
        self._pending = None
        if self._fault is FakeRunnerFault.REJECT_GENERATION:
            self._queue(RejectedFrame(frame.correlation))
            return
        correlation = (
            _different_correlation(frame.correlation)
            if self._fault is FakeRunnerFault.WRONG_RESULT_CORRELATION
            else frame.correlation
        )
        self._queue(ResultFrame(correlation, self._candidates))

    def _queue(self, frame: ChfRunnerFrame) -> None:
        self._queue_raw(encode_chf_runner_frame(frame))

    def _queue_raw(self, raw: bytes) -> None:
        with self._condition:
            self._buffer.extend(raw)
            self._condition.notify_all()


def _different_correlation(correlation: AttemptCorrelation) -> AttemptCorrelation:
    return replace(correlation, correlation_id="request:" + "f" * 32)
