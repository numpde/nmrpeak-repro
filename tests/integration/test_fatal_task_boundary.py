"""Contain fatal scientific exceptions long enough to settle their original owners."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.hello import AnalysisOffering

from nmrpeak_provider.analysis_session import NmrpeakAnalysisSession
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.generation_runtime import GenerationRuntime
from nmrpeak_provider.provider_config import ObservationPolicy, ProviderProcessPolicy
from nmrpeak_provider.provider_process import ProviderAnnouncement, ProviderProcess
from tests.fakes.shared_api import SharedApi
from tests.unit.test_analysis_session import interpreter, scientific_lane
from tests.unit.test_input_interpreter import HF_VALUE


async def fatal_science_probe():
    async with AsyncExitStack() as owners:
        events = []
        hf, hf_runner, hf_channel, _ = await owners.enter_async_context(scientific_lane("hf"))
        chf, chf_runner, _, _ = await owners.enter_async_context(scientific_lane("chf"))
        runtime = GenerationRuntime("sha256:" + "1" * 64, hf, chf)
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, canonical_json_bytes(HF_VALUE))})

        def fatal_generation(_frame):
            events.append("fatal_science_entered")
            raise SystemExit("Scientific operation terminated unexpectedly")

        hf_channel._accept_generate = fatal_generation
        original_retire = NmrpeakAnalysisSession.retire

        async def retire(owner):
            await original_retire(owner)
            events.append("original_attempt_retired")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").mkdir(mode=0o700)
            (root / "ipc").mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "ipc/report.sock")
            queue = DeliveryQueue(root / "state", limits=QueueLimits(4, 1048576, 4, 1048576))
            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2,
                                  on_accepted=Mock(), on_failure=Mock(),
                                  binding_status=lambda _endpoint: {"state": "inbox_only"})
            await inbox.start()
            service = ProviderProcess(
                runtime=runtime, hf_session=hf_runner, chf_session=chf_runner,
                interpreter=interpreter(), new_api=api.new_api,
                new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
                policy=ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30)),
                announcement=ProviderAnnouncement("NMRPeak", "Fatal-boundary fixture", tuple(
                    AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering")
                    for lane in (hf, chf))),
            )
            try:
                with patch.object(NmrpeakAnalysisSession, "retire", retire):
                    try:
                        await service.run(on_ready=Mock())
                    except BaseException:
                        events.append("provider_observed_failure")
                if not service.shutdown_ready:
                    raise RuntimeError("Original provider owners did not settle after fatal science")
                if "original_attempt_retired" not in events:
                    raise RuntimeError("Fatal science bypassed the original Attempt retirement")
                if queue.inventory(api.origin, api.provider_ref):
                    raise RuntimeError("Unclassified fatal science fabricated a terminal report")
                if any(path.endswith(("/complete", "/fail")) for _, path, _ in api.calls):
                    raise RuntimeError("Provider directly published an unclassified fatal failure")
            finally:
                await inbox.close()
                await hf_runner.retire()
                await chf_runner.retire()
        if "fatal_science_entered" not in events:
            raise RuntimeError("Fatal-boundary fixture did not enter actual SCI generation")

class FatalTaskBoundaryTests(unittest.TestCase):
    def test_fatal_science_does_not_escape_the_loop_before_original_retirement(self):
        # KI/SystemExit are tested in a subprocess because a broken task boundary
        # stops asyncio's loop before an ordinary in-loop assertion can run.
        result = subprocess.run(
            (sys.executable, "-m", __name__, "--probe"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__" and sys.argv[1:] == ["--probe"]:
    try:
        asyncio.run(fatal_science_probe())
    except BaseException as failure:
        print("Fatal operation escaped the asyncio boundary: " + type(failure).__name__)
        raise SystemExit(1) from None
