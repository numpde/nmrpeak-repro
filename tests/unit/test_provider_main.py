"""Prove production composition admits local inputs before locked provider work."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
import signal
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from nmrpeak_provider.frozen_generation import FrozenGeneration
from nmrpeak_provider.provider_main import (
    _prepare_hello,
    main,
    run_provider,
)
from nmrpeak_provider.provider_process import (
    ProviderLaneFailed,
    ProviderProtocolFailed,
)
from nmrpeak_provider.provider_credential import parse_provider_signing_credential
from tests.unit.test_frozen_generation import FILES, runtime
from tests.unit.test_provider_config import CONFIG
from tests.unit.test_provider_startup_inputs import credential_bytes


class FakeSession:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.retired = False

    def retire(self) -> None:
        self.retired = True
        self.events.append(f"{self.name}_retired")


class FakeJournal:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("journal_closed")


class ProviderMainTests(unittest.TestCase):
    def test_main_renders_process_effect_cause_and_cleanup_note(self) -> None:
        cause = ProviderProtocolFailed(
            "Cannot read the Job feed: the HTTP 403 problem response failed validation."
        )
        failure = ProviderLaneFailed(
            "The hf provider lane stopped unexpectedly, so coordinated provider "
            "shutdown began."
        )
        failure.__cause__ = cause
        failure.add_note("A sibling runner session also failed to close.")
        stderr = StringIO()

        with (
            patch("nmrpeak_provider.provider_main.run_provider", side_effect=failure),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 1)

        rendered = stderr.getvalue()
        self.assertIn("Provider process stopped unexpectedly", rendered)
        self.assertIn("coordinated provider shutdown began", rendered)
        self.assertIn("Cause: Cannot read the Job feed", rendered)
        self.assertIn("shutdown could not confirm every local resource closure", rendered)
        self.assertIn("provider is not ready", rendered)

    def test_main_does_not_publish_unowned_exception_text(self) -> None:
        failure = RuntimeError("api_key=must-not-reach-operator-stderr")
        failure.add_note("credential=must-not-reach-operator-stderr")
        stderr = StringIO()

        with (
            patch("nmrpeak_provider.provider_main.run_provider", side_effect=failure),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 1)

        rendered = stderr.getvalue()
        self.assertIn("unexpected internal error", rendered)
        self.assertNotIn("must-not-reach-operator-stderr", rendered)

    def test_hello_uses_only_the_two_authenticated_frozen_descriptions(self) -> None:
        frozen = FrozenGeneration("sha256:" + "1" * 64, runtime(), FILES)

        prepared = _prepare_hello(frozen)
        document = parse_canonical_json_bytes(prepared.body)

        self.assertEqual(
            [item["description"] for item in document["analysis_offerings"]],
            [frozen_file.content.decode() for frozen_file in FILES[:2]],
        )
        self.assertIn("NMR peak-list input", document["description"])

    def test_local_admission_precedes_locked_work_and_cleanup_surrounds_failure(self) -> None:
        events: list[str] = []
        frozen = FrozenGeneration("sha256:" + "1" * 64, runtime(), FILES)
        credential_raw = credential_bytes(Ed25519PrivateKey.generate())
        credential = parse_provider_signing_credential(credential_raw)

        class EndpointConfig:
            def materialize(self) -> object:
                events.append("tls_materialized")
                return object()

        configured = SimpleNamespace(
            frozen_generation_id=frozen.frozen_generation_id,
            runner=object(),
            journal_maximum_records=2,
            journal_filesystem_reserve_bytes=1024,
            endpoint=EndpointConfig(),
            process=object(),
            interpreter=object(),
        )
        hf = FakeSession("hf", events)
        chf = FakeSession("chf", events)
        journal = FakeJournal(events)

        class HeldLock:
            def __enter__(self) -> HeldLock:
                events.append("lock_entered")
                return self

            def __exit__(self, *exc_info: object) -> None:
                events.append("lock_closed")

        def load_generation(*_args: object, **_kwargs: object) -> FrozenGeneration:
            events.append("generation_loaded")
            return frozen

        def open_session(socket_path: str, *_args: object) -> FakeSession:
            name = "hf" if "/hf/" in socket_path else "chf"
            events.append(f"{name}_opened")
            return hf if name == "hf" else chf

        def open_journal(*_args: object, **_kwargs: object) -> FakeJournal:
            events.append("journal_opened")
            return journal

        def acquire_lock(*_args: object) -> HeldLock:
            events.append("lock_acquired")
            return HeldLock()

        def create_api(*_args: object) -> object:
            events.append("api_created")
            return object()

        def enter_process(**_kwargs: object) -> None:
            events.append("provider_process_entered")
            _kwargs["on_ready"]()
            raise RuntimeError("provider failed")

        class FakeReadiness:
            def publish(self) -> None:
                events.append("readiness_published")

            def close(self) -> None:
                events.append("readiness_closed")

        def begin_readiness() -> FakeReadiness:
            events.append("readiness_begun")
            return FakeReadiness()

        with (
            patch(
                "nmrpeak_provider.provider_main.ProviderReadiness.begin",
                side_effect=begin_readiness,
            ),
            patch(
                "nmrpeak_provider.provider_main._read_regular_file",
                side_effect=(CONFIG, credential_raw),
            ),
            patch(
                "nmrpeak_provider.provider_main.decode_provider_runtime_config",
                return_value=configured,
            ),
            patch(
                "nmrpeak_provider.provider_main.load_openai_chat_endpoint_specs",
                return_value=(object(),),
            ),
            patch(
                "nmrpeak_provider.provider_main.InputInterpreter",
                return_value=object(),
            ),
            patch(
                "nmrpeak_provider.provider_main.load_frozen_generation",
                side_effect=load_generation,
            ),
            patch(
                "nmrpeak_provider.provider_main.parse_provider_signing_credential",
                return_value=credential,
            ),
            patch(
                "nmrpeak_provider.provider_main.open_runner_session",
                side_effect=open_session,
            ),
            patch(
                "nmrpeak_provider.provider_main.AttemptJournalStore",
                side_effect=open_journal,
            ),
            patch(
                "nmrpeak_provider.provider_main.ProviderIdentityLock.acquire",
                side_effect=acquire_lock,
            ),
            patch(
                "nmrpeak_provider.provider_main.ProviderApiClient",
                side_effect=create_api,
            ),
            patch(
                "nmrpeak_provider.provider_main.run_provider_process",
                side_effect=enter_process,
            ),
            patch(
                "nmrpeak_provider.provider_main.signal.signal",
                return_value=signal.SIG_DFL,
            ),
            self.assertRaisesRegex(RuntimeError, "provider failed"),
        ):
            run_provider()

        self.assertEqual(
            events,
            [
                "readiness_begun",
                "generation_loaded",
                "lock_acquired",
                "lock_entered",
                "tls_materialized",
                "api_created",
                "hf_opened",
                "chf_opened",
                "journal_opened",
                "provider_process_entered",
                "readiness_published",
                "journal_closed",
                "hf_retired",
                "chf_retired",
                "lock_closed",
                "readiness_closed",
            ],
        )


if __name__ == "__main__":
    unittest.main()
