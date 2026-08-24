"""The closed canonical frame protocol for one CHF runner boot."""

from __future__ import annotations

from dataclasses import dataclass
import re
from struct import pack, unpack
from typing import Protocol, TypeAlias, cast

from .canonical_json import (
    CanonicalJsonError,
    JsonValue,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from .chf_binding import ChfRunnerInput, parse_chf_runner_input


RUNNER_PROTOCOL_VERSION = 1
CHF_RUNNER_CONTRACT_ID = "nmrpeak.runner_session.chf.v1"
MAX_CHF_FRAME_PAYLOAD_BYTES = 131_072
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_BOOT_GENERATION = re.compile(r"boot:[0-9a-f]{32}")
_CORRELATION_ID = re.compile(r"request:[0-9a-f]{32}")
_ATTEMPT_REF = re.compile(r"execution_attempt:sha256:[0-9a-f]{64}")
_PROVIDER_ATTEMPT_KEY = re.compile(r"nmrpeak-provider\.v1:[0-9a-f]{64}")


class ChfRunnerProtocolError(ValueError):
    """A private frame cannot participate in the current runner boot."""


class _FrameReceiver(Protocol):
    def recv_into(self, buffer: memoryview) -> int: ...


@dataclass(frozen=True, slots=True)
class AttemptCorrelation:
    """Provider-owned facts binding one request and every response to an Attempt."""

    boot_generation: str
    correlation_id: str
    attempt_ref: str
    provider_attempt_key: str

    def __post_init__(self) -> None:
        _require_match(self.boot_generation, _BOOT_GENERATION, "boot generation")
        _require_match(self.correlation_id, _CORRELATION_ID, "correlation ID")
        _require_match(self.attempt_ref, _ATTEMPT_REF, "Attempt ref")
        _require_match(
            self.provider_attempt_key,
            _PROVIDER_ATTEMPT_KEY,
            "provider Attempt key",
        )


@dataclass(frozen=True, slots=True)
class ReadyFrame:
    """Measured runner facts offered for provider admission after boot."""

    boot_generation: str
    runner_ref: str
    runner_contract_id: str
    release_sha256: str
    source_closure_sha256: str
    image_input_id: str
    target: str
    device: str
    decode_policy_id: str

    def __post_init__(self) -> None:
        _require_match(self.boot_generation, _BOOT_GENERATION, "boot generation")
        for value, name in (
            (self.runner_ref, "runner ref"),
            (self.runner_contract_id, "runner contract ID"),
            (self.target, "target"),
            (self.device, "device"),
            (self.decode_policy_id, "decode policy ID"),
        ):
            _require_text(value, name)
        for value, name in (
            (self.release_sha256, "release SHA-256"),
            (self.source_closure_sha256, "source closure SHA-256"),
            (self.image_input_id, "image input ID"),
        ):
            _require_match(value, _SHA256_REF, name)


@dataclass(frozen=True, slots=True)
class ValidateFrame:
    """One Attempt-bound request for checkpoint-free tokenizer admission."""

    correlation: AttemptCorrelation
    model_input: ChfRunnerInput

    def __post_init__(self) -> None:
        _require_correlation(self.correlation)
        if type(self.model_input) is not ChfRunnerInput:
            raise TypeError("CHF runner VALIDATE frame requires an owned model input")


@dataclass(frozen=True, slots=True)
class ValidatedFrame:
    """The runner accepted one complete input for generation on this boot."""

    correlation: AttemptCorrelation

    def __post_init__(self) -> None:
        _require_correlation(self.correlation)


@dataclass(frozen=True, slots=True)
class GenerateFrame:
    """The provider authorizes model execution for the validated input."""

    correlation: AttemptCorrelation

    def __post_init__(self) -> None:
        _require_correlation(self.correlation)


@dataclass(frozen=True, slots=True)
class RejectedFrame:
    """A deterministic, reusable rejection of a fully parsed model input."""

    correlation: AttemptCorrelation
    reason: str = "input_rejected"

    def __post_init__(self) -> None:
        _require_correlation(self.correlation)
        if self.reason != "input_rejected":
            raise ChfRunnerProtocolError(
                "Cannot bind CHF runner REJECTED frame: reason is not supported"
            )


@dataclass(frozen=True, slots=True)
class ResultFrame:
    """Untrusted candidate data returned for provider-owned result validation."""

    correlation: AttemptCorrelation
    candidates: JsonValue

    def __post_init__(self) -> None:
        _require_correlation(self.correlation)


@dataclass(frozen=True, slots=True)
class RetireFrame:
    """The provider asks an idle runner to terminate this exact boot."""

    boot_generation: str

    def __post_init__(self) -> None:
        _require_match(self.boot_generation, _BOOT_GENERATION, "boot generation")


ChfRunnerFrame: TypeAlias = (
    ReadyFrame
    | ValidateFrame
    | ValidatedFrame
    | GenerateFrame
    | RejectedFrame
    | ResultFrame
    | RetireFrame
)


def encode_chf_runner_frame(frame: ChfRunnerFrame) -> bytes:
    """Render one length-prefixed canonical frame within the fixed byte cap."""

    payload = canonical_json_bytes(_frame_document(frame))
    if len(payload) > MAX_CHF_FRAME_PAYLOAD_BYTES:
        raise ChfRunnerProtocolError(
            "Cannot send CHF runner frame: canonical payload exceeds 131072 bytes"
        )
    return pack(">I", len(payload)) + payload


def parse_chf_frame_header(header: bytes) -> int:
    """Admit a declared payload length before the receiver allocates its body."""

    if type(header) is not bytes or len(header) != 4:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: the four-byte header is incomplete"
        )
    payload_length = unpack(">I", header)[0]
    if payload_length > MAX_CHF_FRAME_PAYLOAD_BYTES:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: declared payload exceeds 131072 bytes"
        )
    return payload_length


def receive_chf_runner_frame(connection: _FrameReceiver) -> ChfRunnerFrame:
    """Read one frame, admitting its length before allocating the payload."""

    header = bytearray(4)
    _receive_exact(connection, memoryview(header), "header")
    payload_length = parse_chf_frame_header(bytes(header))
    payload = bytearray(payload_length)
    _receive_exact(connection, memoryview(payload), "payload")
    return decode_chf_runner_payload(bytes(payload))


def decode_chf_runner_frame(raw: bytes) -> ChfRunnerFrame:
    """Parse one complete length-prefixed frame with no trailing bytes."""

    if type(raw) is not bytes:
        raise TypeError("CHF runner frame must be supplied as exact bytes")
    if len(raw) < 4:
        parse_chf_frame_header(raw)
    payload_length = parse_chf_frame_header(raw[:4])
    if len(raw) != 4 + payload_length:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: payload length does not match its header"
        )
    return decode_chf_runner_payload(raw[4:])


def decode_chf_runner_payload(payload: bytes) -> ChfRunnerFrame:
    """Parse an already bounded payload into its exact directional schema."""

    if type(payload) is not bytes:
        raise TypeError("CHF runner payload must be supplied as exact bytes")
    if len(payload) > MAX_CHF_FRAME_PAYLOAD_BYTES:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: payload exceeds 131072 bytes"
        )
    try:
        value = parse_canonical_json_bytes(payload)
    except CanonicalJsonError as error:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: payload is not canonical UTF-8 JSON"
        ) from error
    document = _object(value, "frame")
    frame_type = document.get("type")
    if frame_type == "READY":
        return _parse_ready(document)
    if frame_type == "VALIDATE":
        return _parse_validate(document)
    if frame_type == "VALIDATED":
        return ValidatedFrame(_parse_correlated(document, {"v", "type"}))
    if frame_type == "GENERATE":
        return GenerateFrame(_parse_correlated(document, {"v", "type"}))
    if frame_type == "REJECTED":
        return _parse_rejected(document)
    if frame_type == "RESULT":
        return _parse_result(document)
    if frame_type == "RETIRE":
        return _parse_retire(document)
    raise ChfRunnerProtocolError(
        "Cannot receive CHF runner frame: frame type is not supported"
    )


def _frame_document(frame: ChfRunnerFrame) -> dict[str, JsonValue]:
    if type(frame) is ReadyFrame:
        return {
            "v": RUNNER_PROTOCOL_VERSION,
            "type": "READY",
            "boot_generation": frame.boot_generation,
            "runner_ref": frame.runner_ref,
            "runner_contract_id": frame.runner_contract_id,
            "release_sha256": frame.release_sha256,
            "source_closure_sha256": frame.source_closure_sha256,
            "image_input_id": frame.image_input_id,
            "target": frame.target,
            "device": frame.device,
            "decode_policy_id": frame.decode_policy_id,
        }
    if type(frame) is ValidateFrame:
        return {
            **_correlation_document(frame.correlation),
            "v": RUNNER_PROTOCOL_VERSION,
            "type": "VALIDATE",
            "model_input": frame.model_input.wire_document(),
        }
    if type(frame) is ValidatedFrame:
        return _correlated_document("VALIDATED", frame.correlation)
    if type(frame) is GenerateFrame:
        return _correlated_document("GENERATE", frame.correlation)
    if type(frame) is RejectedFrame:
        return {
            **_correlated_document("REJECTED", frame.correlation),
            "reason": frame.reason,
        }
    if type(frame) is ResultFrame:
        return {
            **_correlated_document("RESULT", frame.correlation),
            "candidates": frame.candidates,
        }
    if type(frame) is RetireFrame:
        return {
            "v": RUNNER_PROTOCOL_VERSION,
            "type": "RETIRE",
            "boot_generation": frame.boot_generation,
        }
    raise TypeError("CHF runner protocol can encode only owned frame types")


def _parse_ready(document: dict[str, JsonValue]) -> ReadyFrame:
    fields = {
        "v", "type", "boot_generation", "runner_ref", "runner_contract_id",
        "release_sha256", "source_closure_sha256", "image_input_id", "target",
        "device", "decode_policy_id",
    }
    _require_fields(document, fields)
    _require_version(document)
    return ReadyFrame(
        boot_generation=_text(document["boot_generation"], "boot generation"),
        runner_ref=_text(document["runner_ref"], "runner ref"),
        runner_contract_id=_text(document["runner_contract_id"], "runner contract ID"),
        release_sha256=cast(str, document["release_sha256"]),
        source_closure_sha256=cast(str, document["source_closure_sha256"]),
        image_input_id=cast(str, document["image_input_id"]),
        target=_text(document["target"], "target"),
        device=_text(document["device"], "device"),
        decode_policy_id=_text(document["decode_policy_id"], "decode policy ID"),
    )


def _parse_validate(document: dict[str, JsonValue]) -> ValidateFrame:
    correlation = _parse_correlated(document, {"v", "type", "model_input"})
    try:
        model_input = parse_chf_runner_input(document["model_input"])
    except ValueError as error:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner VALIDATE frame: model input schema is invalid"
        ) from error
    return ValidateFrame(correlation, model_input)


def _parse_rejected(document: dict[str, JsonValue]) -> RejectedFrame:
    correlation = _parse_correlated(document, {"v", "type", "reason"})
    if document["reason"] != "input_rejected":
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner REJECTED frame: reason is not supported"
        )
    return RejectedFrame(correlation)


def _parse_result(document: dict[str, JsonValue]) -> ResultFrame:
    correlation = _parse_correlated(document, {"v", "type", "candidates"})
    return ResultFrame(correlation, document["candidates"])


def _parse_retire(document: dict[str, JsonValue]) -> RetireFrame:
    _require_fields(document, {"v", "type", "boot_generation"})
    _require_version(document)
    boot_generation = _text(document["boot_generation"], "boot generation")
    _require_match(boot_generation, _BOOT_GENERATION, "boot generation")
    return RetireFrame(boot_generation)


def _parse_correlated(
    document: dict[str, JsonValue],
    frame_fields: set[str],
) -> AttemptCorrelation:
    correlation_fields = {
        "boot_generation", "correlation_id", "attempt_ref", "provider_attempt_key"
    }
    _require_fields(document, frame_fields | correlation_fields)
    _require_version(document)
    try:
        return AttemptCorrelation(
            boot_generation=_text(document["boot_generation"], "boot generation"),
            correlation_id=_text(document["correlation_id"], "correlation ID"),
            attempt_ref=_text(document["attempt_ref"], "Attempt ref"),
            provider_attempt_key=_text(
                document["provider_attempt_key"], "provider Attempt key"
            ),
        )
    except (TypeError, ValueError) as error:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: Attempt correlation is invalid"
        ) from error


def _correlated_document(
    frame_type: str,
    correlation: AttemptCorrelation,
) -> dict[str, JsonValue]:
    return {
        **_correlation_document(correlation),
        "v": RUNNER_PROTOCOL_VERSION,
        "type": frame_type,
    }


def _correlation_document(correlation: AttemptCorrelation) -> dict[str, JsonValue]:
    if type(correlation) is not AttemptCorrelation:
        raise TypeError("CHF runner frame requires an owned Attempt correlation")
    return {
        "boot_generation": correlation.boot_generation,
        "correlation_id": correlation.correlation_id,
        "attempt_ref": correlation.attempt_ref,
        "provider_attempt_key": correlation.provider_attempt_key,
    }


def _object(value: object, name: str) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise ChfRunnerProtocolError(
            f"Cannot receive CHF runner frame: {name} must be an object"
        )
    return cast(dict[str, JsonValue], value)


def _require_fields(document: dict[str, JsonValue], fields: set[str]) -> None:
    if set(document) != fields:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: object fields are not exact"
        )


def _require_version(document: dict[str, JsonValue]) -> None:
    if document["v"] != RUNNER_PROTOCOL_VERSION:
        raise ChfRunnerProtocolError(
            "Cannot receive CHF runner frame: protocol version is not supported"
        )


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ChfRunnerProtocolError(
            f"Cannot receive CHF runner frame: {name} must be non-empty text"
        )
    return value


def _require_match(value: object, pattern: re.Pattern[str], name: str) -> None:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ChfRunnerProtocolError(
            f"Cannot bind CHF runner frame: {name} has an invalid format"
        )


def _require_text(value: object, name: str) -> None:
    if type(value) is not str or not value:
        raise ChfRunnerProtocolError(
            f"Cannot bind CHF runner frame: {name} must be non-empty text"
        )


def _require_correlation(value: object) -> None:
    if type(value) is not AttemptCorrelation:
        raise TypeError("CHF runner frame requires an owned Attempt correlation")


def _receive_exact(
    connection: _FrameReceiver,
    destination: memoryview,
    part: str,
) -> None:
    received = 0
    while received < len(destination):
        try:
            count = connection.recv_into(destination[received:])
        except OSError as error:
            raise ChfRunnerProtocolError(
                f"Cannot receive CHF runner frame: socket read failed during {part}"
            ) from error
        if count == 0:
            raise ChfRunnerProtocolError(
                f"Cannot receive CHF runner frame: connection closed during {part}"
            )
        received += count
