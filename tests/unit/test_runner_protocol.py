"""Prove shared runner framing through one concrete model-input codec."""

from __future__ import annotations

from struct import pack
import unittest

from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
)
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CONTRACT_ID,
    CHF_RUNNER_CODEC,
)
from nmrpeak_provider.runner_protocol import (
    MAX_RUNNER_FRAME_PAYLOAD_BYTES,
    AttemptCorrelation,
    RunnerProtocolError,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    ValidateFrame,
    ValidatedFrame,
    parse_frame_header,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
BOOT = "boot:" + "1" * 32
CORRELATION = AttemptCorrelation(
    boot_generation=BOOT,
    correlation_id="request:" + "2" * 32,
    attempt_ref="execution_attempt:sha256:" + "4" * 64,
    provider_attempt_key="nmrpeak-provider.v1:" + "3" * 64,
)
MODEL_INPUT = ChfRunnerInput(
    molecular_formula="C2H6O",
    proton_peaks=(RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
    carbon_peaks=(ChfRunnerCarbonPeak("70.4"),),
)


class BytesReceiver:
    def __init__(self, content: bytes, *, chunk_size: int = 1_000_000) -> None:
        self.content = content
        self.chunk_size = chunk_size
        self.requested_sizes: list[int] = []

    def recv_into(self, destination: memoryview) -> int:
        self.requested_sizes.append(len(destination))
        count = min(len(destination), len(self.content), self.chunk_size)
        destination[:count] = self.content[:count]
        self.content = self.content[count:]
        return count


class RunnerProtocolTests(unittest.TestCase):
    def test_every_protocol_frame_has_one_canonical_round_trip(self) -> None:
        frames = (
            ReadyFrame(
                boot_generation=BOOT,
                runner_ref="nmrpeak_chf_v1",
                runner_contract_id=CHF_RUNNER_CONTRACT_ID,
                release_sha256=SHA_A,
                source_closure_sha256=SHA_B,
                image_input_id=SHA_C,
                target="cpu-x86_64",
                device="cpu",
                decode_policy_id="nmrpeak_chf_decode_v1",
            ),
            ValidateFrame(CORRELATION, MODEL_INPUT),
            ValidatedFrame(CORRELATION),
            GenerateFrame(CORRELATION),
            RejectedFrame(CORRELATION),
            ResultFrame(CORRELATION, ["CCO", "OCC"]),
            RetireFrame(BOOT),
        )

        for frame in frames:
            with self.subTest(frame_type=type(frame).__name__):
                encoded = CHF_RUNNER_CODEC.encode(frame)
                self.assertEqual(len(encoded) - 4, parse_frame_header(encoded[:4]))
                self.assertEqual(frame, CHF_RUNNER_CODEC.decode_frame(encoded))

    def test_length_prefix_rejects_oversize_partial_and_trailing_payloads(self) -> None:
        with self.assertRaisesRegex(RunnerProtocolError, "exceeds 131072"):
            parse_frame_header(pack(">I", MAX_RUNNER_FRAME_PAYLOAD_BYTES + 1))
        for raw in (
            b"\x00\x00\x00",
            pack(">I", 3) + b"{}",
            pack(">I", 2) + b"{}x",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(RunnerProtocolError):
                    CHF_RUNNER_CODEC.decode_frame(raw)

        oversized_result = ResultFrame(
            CORRELATION,
            ["x" * MAX_RUNNER_FRAME_PAYLOAD_BYTES],
        )
        with self.assertRaisesRegex(RunnerProtocolError, "Cannot send"):
            CHF_RUNNER_CODEC.encode(oversized_result)

    def test_bounded_reader_checks_length_before_allocating_payload(self) -> None:
        receiver = BytesReceiver(pack(">I", MAX_RUNNER_FRAME_PAYLOAD_BYTES + 1))

        with self.assertRaisesRegex(RunnerProtocolError, "declared payload"):
            CHF_RUNNER_CODEC.receive(receiver)
        self.assertEqual([4], receiver.requested_sizes)

    def test_bounded_reader_reports_eof_inside_a_declared_payload(self) -> None:
        receiver = BytesReceiver(pack(">I", 5) + b"{}", chunk_size=4)

        with self.assertRaisesRegex(RunnerProtocolError, "closed during payload"):
            CHF_RUNNER_CODEC.receive(receiver)

    def test_canonical_json_exact_fields_and_correlation_are_mandatory(self) -> None:
        noncanonical = (
            b'{"v":1, "type":"RETIRE","boot_generation":"' 
            + BOOT.encode("ascii")
            + b'"}'
        )
        unknown_field = canonical_json_bytes(
            {
                "v": 1,
                "type": "RETIRE",
                "boot_generation": BOOT,
                "extra": None,
            }
        )
        wrong_correlation = canonical_json_bytes(
            {
                "v": 1,
                "type": "VALIDATED",
                "boot_generation": BOOT,
                "correlation_id": "request:not-hex",
                "attempt_ref": CORRELATION.attempt_ref,
                "provider_attempt_key": CORRELATION.provider_attempt_key,
            }
        )
        for payload in (noncanonical, unknown_field, wrong_correlation):
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(RunnerProtocolError):
                    CHF_RUNNER_CODEC.decode_payload(payload)

    def test_result_candidates_remain_untrusted_for_the_product_validator(self) -> None:
        frame = ResultFrame(CORRELATION, {"not": "a candidate array"})

        decoded = CHF_RUNNER_CODEC.decode_frame(CHF_RUNNER_CODEC.encode(frame))

        self.assertEqual(frame, decoded)


if __name__ == "__main__":
    unittest.main()
