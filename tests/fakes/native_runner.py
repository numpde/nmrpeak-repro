"""An SDK-owned child using the production HF/CHF science codecs, without weights."""

import argparse
from dataclasses import replace
import os
import signal
from pathlib import Path
import socket
import time

from families.nmrpeak.runner_worker import serve_loaded_nmrpeak_runtime
from nmrpeak_provider.hf_binding import HfRunnerInput
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CODEC
from nmrpeak_provider.product_result import HF_RESULT_IDENTITY
from tests.unit.test_runner_session import FACTS, MODEL_INPUT, ready_frame


def lane_contract(lane):
    facts, codec, model_input = FACTS, CHF_RUNNER_CODEC, MODEL_INPUT
    if lane == "hf":
        facts = replace(FACTS, identity=HF_RESULT_IDENTITY, runner_contract_id=HF_RUNNER_CONTRACT_ID)
        codec = HF_RUNNER_CODEC
        model_input = HfRunnerInput(MODEL_INPUT.molecular_formula, MODEL_INPUT.proton_peaks)
    ready = replace(
        ready_frame(), runner_ref=facts.identity.runner_ref,
        runner_contract_id=facts.runner_contract_id,
        decode_policy_id=facts.identity.decode_policy.decode_policy_id,
    )
    return facts, codec, ready, model_input


class Runtime:
    def __init__(self, mode, connection):
        self.mode, self.connection = mode, connection

    def validate(self, model_input):
        pass

    def generate(self, model_input):
        if self.mode == "crash":
            os._exit(17)
        if self.mode == "reported_failure":
            raise RuntimeError("Scientific fixture failed")
        if self.mode == "eof_alive":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            time.sleep(60)
        return ["CCO"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=("hf", "chf"), required=True)
    parser.add_argument("--session-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--generation-mode", choices=("healthy", "crash", "reported_failure", "eof_alive"), default="healthy")
    options = parser.parse_args()
    _, codec, ready, _ = lane_contract(options.lane)
    Path("child.pid").write_text(str(os.getpid()))
    with socket.socket(fileno=options.session_fd) as connection:
        return serve_loaded_nmrpeak_runtime(
            connection, Runtime(options.generation_mode, connection),
            replace(ready, boot_generation="boot:" + options.operation), codec,
        )


if __name__ == "__main__":
    raise SystemExit(main())
