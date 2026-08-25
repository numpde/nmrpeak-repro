"""Prove the HF codec closes the shared framing around HF input only."""

from __future__ import annotations

import unittest

from nmrpeak_provider.chf_binding import ChfRunnerCarbonPeak, ChfRunnerInput
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CODEC
from nmrpeak_provider.hf_binding import HfRunnerInput
from nmrpeak_provider.hf_runner_protocol import (
    HF_RUNNER_CODEC,
)
from nmrpeak_provider.runner_protocol import (
    AttemptCorrelation,
    RunnerProtocolError,
    ValidateFrame,
)
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak


CORRELATION = AttemptCorrelation(
    boot_generation="boot:" + "a" * 32,
    correlation_id="request:" + "b" * 32,
    attempt_ref="execution_attempt:sha256:" + "c" * 64,
    provider_attempt_key="nmrpeak-provider.v1:" + "d" * 64,
)
PROTON = RunnerProtonPeak("1.25", 3, "t", "7.1_")


class HfRunnerProtocolTests(unittest.TestCase):
    def test_hf_validate_frame_has_one_canonical_round_trip(self) -> None:
        frame = ValidateFrame(CORRELATION, HfRunnerInput("C2H6O", (PROTON,)))

        self.assertEqual(frame, HF_RUNNER_CODEC.decode_frame(HF_RUNNER_CODEC.encode(frame)))

    def test_each_codec_rejects_the_other_lanes_model_input(self) -> None:
        hf_frame = ValidateFrame(CORRELATION, HfRunnerInput("C2H6O", (PROTON,)))
        chf_frame = ValidateFrame(
            CORRELATION,
            ChfRunnerInput(
                "C2H6O",
                (PROTON,),
                (ChfRunnerCarbonPeak("70.4"),),
            ),
        )

        with self.assertRaisesRegex(TypeError, "CHF runner protocol"):
            CHF_RUNNER_CODEC.encode(hf_frame)
        with self.assertRaisesRegex(TypeError, "HF runner protocol"):
            HF_RUNNER_CODEC.encode(chf_frame)

    def test_hf_decoder_rejects_a_canonical_chf_input_frame(self) -> None:
        chf_frame = ValidateFrame(
            CORRELATION,
            ChfRunnerInput(
                "C2H6O",
                (PROTON,),
                (ChfRunnerCarbonPeak("70.4"),),
            ),
        )

        with self.assertRaisesRegex(RunnerProtocolError, "HF runner VALIDATE"):
            HF_RUNNER_CODEC.decode_frame(CHF_RUNNER_CODEC.encode(chf_frame))


if __name__ == "__main__":
    unittest.main()
