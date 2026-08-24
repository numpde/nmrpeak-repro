"""Prove the closed CHF schemas and pre-allocation framing boundary."""

from __future__ import annotations

from struct import pack
import unittest

from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
    ChfRunnerProtonPeak,
)
from nmrpeak_provider.chf_runner_protocol import (
    MAX_CHF_FRAME_PAYLOAD_BYTES,
    AttemptCorrelation,
    ChfRunnerProtocolError,
    GenerateFrame,
    ReadyFrame,
    RejectedFrame,
    ResultFrame,
    RetireFrame,
    ValidateFrame,
    ValidatedFrame,
    decode_chf_runner_frame,
    decode_chf_runner_payload,
    encode_chf_runner_frame,
    parse_chf_frame_header,
    receive_chf_runner_frame,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
BOOT = "boot:" + "1" * 32
CORRELATION = AttemptCorrelation(
    boot_generation=BOOT,
    correlation_id="request:" + "2" * 32,
    attempt_ref="attempt:chf-7",
    provider_attempt_key="nmrpeak-provider.v1:" + "3" * 64,
)
MODEL_INPUT = ChfRunnerInput(
    molecular_formula="C2H6O",
    proton_peaks=(ChfRunnerProtonPeak("1.25", 3, "t", "7.1_"),),
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


class ChfRunnerProtocolTests(unittest.TestCase):
    def test_every_protocol_frame_has_one_canonical_round_trip(self) -> None:
        frames = (
            ReadyFrame(
                boot_generation=BOOT,
                runner_ref="nmrpeak_chf_v1",
                runner_contract_id="nmrpeak.runner_session.chf.v1",
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
                encoded = encode_chf_runner_frame(frame)
                self.assertEqual(len(encoded) - 4, parse_chf_frame_header(encoded[:4]))
                self.assertEqual(frame, decode_chf_runner_frame(encoded))

    def test_length_prefix_rejects_oversize_partial_and_trailing_payloads(self) -> None:
        with self.assertRaisesRegex(ChfRunnerProtocolError, "exceeds 131072"):
            parse_chf_frame_header(pack(">I", MAX_CHF_FRAME_PAYLOAD_BYTES + 1))
        for raw in (
            b"\x00\x00\x00",
            pack(">I", 3) + b"{}",
            pack(">I", 2) + b"{}x",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ChfRunnerProtocolError):
                    decode_chf_runner_frame(raw)

        oversized_result = ResultFrame(
            CORRELATION,
            ["x" * MAX_CHF_FRAME_PAYLOAD_BYTES],
        )
        with self.assertRaisesRegex(ChfRunnerProtocolError, "Cannot send"):
            encode_chf_runner_frame(oversized_result)

    def test_bounded_reader_checks_length_before_allocating_payload(self) -> None:
        receiver = BytesReceiver(pack(">I", MAX_CHF_FRAME_PAYLOAD_BYTES + 1))

        with self.assertRaisesRegex(ChfRunnerProtocolError, "declared payload"):
            receive_chf_runner_frame(receiver)
        self.assertEqual([4], receiver.requested_sizes)

    def test_bounded_reader_reports_eof_inside_a_declared_payload(self) -> None:
        receiver = BytesReceiver(pack(">I", 5) + b"{}", chunk_size=4)

        with self.assertRaisesRegex(ChfRunnerProtocolError, "closed during payload"):
            receive_chf_runner_frame(receiver)

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
                with self.assertRaises(ChfRunnerProtocolError):
                    decode_chf_runner_payload(payload)

    def test_result_candidates_remain_untrusted_for_the_product_validator(self) -> None:
        frame = ResultFrame(CORRELATION, {"not": "a candidate array"})

        decoded = decode_chf_runner_frame(encode_chf_runner_frame(frame))

        self.assertEqual(frame, decoded)


if __name__ == "__main__":
    unittest.main()
