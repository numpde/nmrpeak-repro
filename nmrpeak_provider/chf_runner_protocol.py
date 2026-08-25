"""The closed CHF model-input codec for the shared runner framing."""

from .chf_binding import ChfRunnerInput, parse_chf_runner_input
from .runner_protocol import RunnerFrameCodec


CHF_RUNNER_CONTRACT_ID = "nmrpeak.runner_session.chf.v1"
CHF_RUNNER_CODEC = RunnerFrameCodec(
    lane_name="CHF",
    model_input_type=ChfRunnerInput,
    parse_model_input=parse_chf_runner_input,
)
