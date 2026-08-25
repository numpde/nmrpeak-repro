"""The closed HF model-input codec for the shared runner framing."""

from .hf_binding import HfRunnerInput, parse_hf_runner_input
from .runner_protocol import RunnerFrameCodec


HF_RUNNER_CONTRACT_ID = "nmrpeak.runner_session.hf.v1"
HF_RUNNER_CODEC = RunnerFrameCodec(
    lane_name="HF",
    model_input_type=HfRunnerInput,
    parse_model_input=parse_hf_runner_input,
)
