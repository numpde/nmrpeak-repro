"""Prove the OpenAI transport's config, retry, deadline, and release boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from nmrpeak_provider.interpreter import InterpreterTransportError
from nmrpeak_provider.local_input import (
    LocalInputFailureReason,
    LocalInputSnapshotError,
)
from nmrpeak_provider.openai_chat_interpreter import (
    OpenAIChatEndpointSpec,
    OpenAIChatEndpoints,
    bind_openai_chat_endpoints,
    load_openai_chat_endpoint_specs,
)
from nmrpeak_provider.interpreter_policy import OpenAIChatCallPolicy


class OpenAIChatInterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_resource_free_redacted_specs_in_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(
                directory / "20-local.toml",
                configuration_id="local",
                api_key="local-secret",
            )
            _write_config(
                directory / "10-remote.toml",
                configuration_id="remote",
                api_key="remote-secret",
            )

            with patch(
                "nmrpeak_provider.openai_chat_interpreter._OpenAIChatCall"
            ) as live_call:
                specs = load_openai_chat_endpoint_specs(directory)

        self.assertEqual(
            [spec.configuration_id for spec in specs],
            ["remote", "local"],
        )
        self.assertTrue(all(type(spec) is OpenAIChatEndpointSpec for spec in specs))
        self.assertNotIn("remote-secret", repr(specs))
        self.assertNotIn("local-secret", repr(specs))
        live_call.assert_not_called()
        with self.assertRaises(FrozenInstanceError):
            specs[0].model = "changed"

    async def test_call_policy_owns_timeout_normalization_and_envelope(self) -> None:
        policy = OpenAIChatCallPolicy(
            request_timeout_seconds=4,
            turn_timeout_seconds=10,
        )
        self.assertEqual(policy.request_timeout_seconds, 4.0)
        self.assertEqual(policy.turn_timeout_seconds, 10.0)

        with self.assertRaisesRegex(ValueError, "must cover every request attempt"):
            OpenAIChatCallPolicy(
                request_timeout_seconds=5,
                turn_timeout_seconds=10,
            )

    async def test_call_policy_rejects_nonpositive_and_nonfinite_timeouts(self) -> None:
        for request_timeout, turn_timeout in (
            (True, 10),
            (0, 10),
            (float("inf"), 10),
            (10**1000, 10),
            (4, False),
            (4, 0),
            (4, float("nan")),
            (4, 10**1000),
        ):
            with self.subTest(
                request_timeout=request_timeout,
                turn_timeout=turn_timeout,
            ), self.assertRaisesRegex(ValueError, "must be positive and finite"):
                OpenAIChatCallPolicy(
                    request_timeout_seconds=request_timeout,
                    turn_timeout_seconds=turn_timeout,
                )

    async def test_duplicate_ids_fail_during_resource_free_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(directory / "10-first.toml", configuration_id="same")
            _write_config(directory / "20-second.toml", configuration_id="same")

            with self.assertRaisesRegex(ValueError, "IDs must be unique"):
                load_openai_chat_endpoint_specs(directory)

    async def test_rejects_empty_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "10-only.toml").write_text(
                'id = "only"\nbase_url = "https://example.test"\n'
                'api_key = ""\nmodel = "model"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid interpreter API key"):
                load_openai_chat_endpoint_specs(directory)

    async def test_ordered_configs_make_standard_chat_completion_calls(self) -> None:
        requests: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=_completion(
                    "submit_interpretation",
                    {"value": {"answer": 1}},
                ),
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(
                directory / "20-local.toml",
                configuration_id="local",
                base_url="http://127.0.0.1:8000/v1/",
                api_key="local-secret",
                model="local-model",
            )
            _write_config(
                directory / "10-remote.toml",
                configuration_id="remote",
                base_url="https://models.example/v1",
                api_key="remote-secret",
                model="remote-model",
                reasoning_effort="none",
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                endpoint_owner = _load_and_bind_endpoints(
                    directory,
                    http_client=client,
                    request_timeout_seconds=4,
                    turn_timeout_seconds=10,
                )
                endpoints = endpoint_owner.endpoints
                turn = await endpoints[0].call(
                    [
                        {"role": "system", "content": "generic"},
                        {"role": "user", "content": "runner"},
                        {"role": "user", "content": "source"},
                    ]
                )
                await endpoints[1].call(
                    [{"role": "system", "content": "generic"}]
                )

        self.assertEqual(
            [endpoint.configuration_id for endpoint in endpoints],
            ["remote", "local"],
        )
        self.assertEqual(
            str(requests[0].url),
            "https://models.example/v1/chat/completions",
        )
        self.assertEqual(
            requests[0].headers["authorization"],
            "Bearer remote-secret",
        )
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "remote-model")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertTrue(
            all(value is None for value in requests[0].extensions["timeout"].values())
        )
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(
            [tool["function"]["name"] for tool in body["tools"]],
            ["submit_interpretation", "report_input_problem"],
        )
        self.assertEqual(
            [message["role"] for message in body["messages"]],
            ["system", "user", "user"],
        )
        self.assertNotIn("reasoning_effort", json.loads(requests[1].content))
        self.assertEqual(turn.invocation.name, "submit_interpretation")
        self.assertEqual(turn.invocation.arguments, {"value": {"answer": 1}})
        self.assertNotIn("remote-secret", repr(endpoints))

    async def test_invalid_arguments_keep_repairable_assistant_turn(self) -> None:
        message = _completion_message(
            "submit_interpretation",
            "not JSON",
            call_id="broken-call",
        )

        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": message}]})

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(directory / "10-only.toml")
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                endpoint = _load_and_bind_endpoints(
                    directory,
                    http_client=client,
                    request_timeout_seconds=4,
                    turn_timeout_seconds=10,
                ).endpoints[0]
                turn = await endpoint.call(
                    [{"role": "system", "content": "generic"}]
                )

        self.assertIsNone(turn.invocation)
        self.assertEqual(turn.tool_call_ids, ("broken-call",))
        self.assertEqual(turn.assistant_message, message)

    async def test_plain_assistant_turn_requires_no_tool_results(self) -> None:
        message = {
            "role": "assistant",
            "content": "I need to correct that response.",
        }

        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": message}]})

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(directory / "10-only.toml")
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                endpoint = _load_and_bind_endpoints(
                    directory,
                    http_client=client,
                    request_timeout_seconds=4,
                    turn_timeout_seconds=10,
                ).endpoints[0]
                turn = await endpoint.call(
                    [{"role": "system", "content": "generic"}]
                )

        self.assertIsNone(turn.invocation)
        self.assertEqual(turn.tool_call_ids, ())
        self.assertEqual(turn.assistant_message, message)

    async def test_retries_operational_failures_at_most_once(self) -> None:
        ambiguous_401 = {
            "error": {
                "type": "invalid_request_error",
                "code": None,
                "param": None,
                "message": "private",
            }
        }
        cases = (
            (
                "intermittent 401 recovers",
                (
                    (401, ambiguous_401),
                    (200, _completion("submit_interpretation", {"value": 7})),
                ),
                None,
            ),
            (
                "connection failure recovers",
                (None, (200, _completion("submit_interpretation", {"value": 7}))),
                None,
            ),
            (
                "persistent 401 is bounded",
                ((401, ambiguous_401),) * 2,
                "http_401",
            ),
            (
                "repeated server failure is bounded",
                ((503, {"error": {"message": "private"}}),) * 2,
                "http_503",
            ),
            (
                "bad request fails immediately",
                ((400, {"error": {"message": "private"}}),),
                "http_400",
            ),
        )

        for name, responses, expected_reason in cases:
            with self.subTest(name):
                requests = 0

                async def handle(_request: httpx.Request) -> httpx.Response:
                    nonlocal requests
                    response = responses[requests]
                    requests += 1
                    if response is None:
                        raise httpx.ConnectError("private", request=_request)
                    status, document = response
                    return httpx.Response(status, json=document)

                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    _write_config(directory / "10-only.toml")
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(handle)
                    ) as client:
                        endpoint = _load_and_bind_endpoints(
                            directory,
                            http_client=client,
                            request_timeout_seconds=4,
                            turn_timeout_seconds=10,
                        ).endpoints[0]
                        with patch(
                            "nmrpeak_provider.openai_chat_interpreter.asyncio.sleep",
                            new=AsyncMock(),
                        ) as sleep:
                            if expected_reason is None:
                                turn = await endpoint.call(
                                    [{"role": "system", "content": "generic"}]
                                )
                                self.assertEqual(
                                    turn.invocation.arguments,
                                    {"value": 7},
                                )
                            else:
                                with self.assertRaises(
                                    InterpreterTransportError
                                ) as raised:
                                    await endpoint.call(
                                        [{"role": "system", "content": "generic"}]
                                    )
                                self.assertEqual(
                                    raised.exception.reason,
                                    expected_reason,
                                )
                                self.assertNotIn("private", str(raised.exception))

                self.assertEqual(requests, len(responses))
                self.assertEqual(sleep.await_count, len(responses) - 1)

    async def test_request_timeout_leaves_turn_budget_for_retry(self) -> None:
        requests = 0

        async def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                await asyncio.Future()
            return httpx.Response(
                200,
                json=_completion("submit_interpretation", {"value": 7}),
            )

        with patch(
            "nmrpeak_provider.openai_chat_interpreter.asyncio.sleep",
            new=AsyncMock(),
        ):
            async with _loaded_endpoints(
                handle,
                request_timeout=0.01,
                turn_timeout=1.1,
            ) as endpoint_owner:
                turn = await endpoint_owner.endpoints[0].call(
                    [{"role": "system", "content": "generic"}]
                )

        self.assertEqual(requests, 2)
        self.assertEqual(turn.invocation.arguments, {"value": 7})

    async def test_rejects_turn_budget_that_cannot_fit_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(directory / "10-only.toml")
            async with httpx.AsyncClient() as client:
                with self.assertRaisesRegex(
                    ValueError,
                    "must cover every request attempt",
                ):
                    _load_and_bind_endpoints(
                        directory,
                        http_client=client,
                        request_timeout_seconds=5,
                        turn_timeout_seconds=10,
                    )

    async def test_request_timeout_does_not_wait_for_response_cleanup(self) -> None:
        stream = _HangingReadAndCloseStream()

        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        with patch(
            "nmrpeak_provider.openai_chat_interpreter.asyncio.sleep",
            new=AsyncMock(),
        ):
            async with _loaded_endpoints(
                handle,
                request_timeout=0.01,
                turn_timeout=1.1,
            ) as endpoint_owner:
                call = asyncio.create_task(
                    endpoint_owner.endpoints[0].call(
                        [{"role": "system", "content": "generic"}]
                    )
                )
                try:
                    done, _pending = await asyncio.wait({call}, timeout=0.2)
                    self.assertIn(
                        call,
                        done,
                        "request cancellation waited for response cleanup",
                    )
                    with self.assertRaises(InterpreterTransportError) as raised:
                        await call
                    self.assertEqual(
                        raised.exception.reason,
                        "endpoint_unavailable",
                    )
                    self.assertTrue(stream.close_started.is_set())
                finally:
                    if not call.done():
                        call.cancel()
                    await asyncio.gather(call, return_exceptions=True)

    async def test_close_error_cannot_replace_primary_read_failure(self) -> None:
        requests = 0

        async def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, stream=_FailingReadAndCloseStream())

        async with _loaded_endpoints(handle) as endpoint_owner:
            with patch(
                "nmrpeak_provider.openai_chat_interpreter.asyncio.sleep",
                new=AsyncMock(),
            ):
                with self.assertRaises(InterpreterTransportError) as raised:
                    await endpoint_owner.endpoints[0].call(
                        [{"role": "system", "content": "generic"}]
                    )

        self.assertEqual(requests, 2)
        self.assertEqual(raised.exception.reason, "endpoint_unavailable")
        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertIn("private read failure", rendered)

    async def test_cancellation_cleanup_is_owned_until_client_teardown(self) -> None:
        stream = _ControlledReadAndCloseStream()

        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        async with _loaded_endpoints(handle) as endpoint_owner:
            call = asyncio.create_task(
                endpoint_owner.endpoints[0].call(
                    [{"role": "system", "content": "generic"}]
                )
            )
            await asyncio.wait_for(stream.read_started.wait(), timeout=0.2)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(call, timeout=0.2)
            await asyncio.wait_for(stream.close_started.wait(), timeout=0.2)
            self.assertFalse(stream.close_finished.is_set())
            stream.release_close.set()
        self.assertTrue(stream.close_finished.is_set())

    async def test_cancellation_during_failure_cleanup_retains_release(self) -> None:
        stream = _FailingReadControlledCloseStream()

        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        async with _loaded_endpoints(handle) as endpoint_owner:
            call = asyncio.create_task(
                endpoint_owner.endpoints[0].call(
                    [{"role": "system", "content": "generic"}]
                )
            )
            await asyncio.wait_for(stream.close_started.wait(), timeout=0.2)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(call, timeout=0.2)
            self.assertFalse(stream.close_finished.is_set())
            stream.release_close.set()
        self.assertTrue(stream.close_finished.is_set())

    async def test_response_close_failure_retries_and_releases_raw_stream(
        self,
    ) -> None:
        body = json.dumps(
            _completion("submit_interpretation", {"value": 7})
        ).encode("utf-8")
        streams = [_FailsFirstCloseStream(body), _FailsFirstCloseStream(body)]
        requests = 0

        async def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            stream = streams[requests]
            requests += 1
            return httpx.Response(200, stream=stream)

        async with _loaded_endpoints(handle) as endpoint_owner:
            with patch(
                "nmrpeak_provider.openai_chat_interpreter.asyncio.sleep",
                new=AsyncMock(),
            ):
                with self.assertRaises(InterpreterTransportError) as raised:
                    await endpoint_owner.endpoints[0].call(
                        [{"role": "system", "content": "generic"}]
                    )

        self.assertEqual(requests, 2)
        self.assertEqual(raised.exception.reason, "endpoint_unavailable")
        self.assertEqual([stream.close_calls for stream in streams], [2, 2])
        self.assertTrue(all(stream.released for stream in streams))

    async def test_non_retryable_status_survives_close_failure(self) -> None:
        stream = _FailsFirstCloseStream(b"")

        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, stream=stream)

        async with _loaded_endpoints(handle) as endpoint_owner:
            with self.assertRaises(InterpreterTransportError) as raised:
                await endpoint_owner.endpoints[0].call(
                    [{"role": "system", "content": "generic"}]
                )

        self.assertEqual(raised.exception.reason, "http_400")
        self.assertEqual(stream.close_calls, 2)
        self.assertTrue(stream.released)

    async def test_unexpected_stream_failure_is_not_endpoint_fallback(self) -> None:
        requests = 0

        async def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, stream=_BrokenStream())

        async with _loaded_endpoints(handle) as endpoint_owner:
            with self.assertRaisesRegex(RuntimeError, "broken stream adapter"):
                await endpoint_owner.endpoints[0].call(
                    [{"role": "system", "content": "generic"}]
                )

        self.assertEqual(requests, 1)

    async def test_invalid_content_encoding_uses_endpoint_fallback(self) -> None:
        requests = 0

        async def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=b"not a gzip stream",
            )

        async with _loaded_endpoints(handle) as endpoint_owner:
            with patch(
                "nmrpeak_provider.openai_chat_interpreter.asyncio.sleep",
                new=AsyncMock(),
            ):
                with self.assertRaises(InterpreterTransportError) as raised:
                    await endpoint_owner.endpoints[0].call(
                        [{"role": "system", "content": "generic"}]
                    )

        self.assertEqual(requests, 2)
        self.assertEqual(raised.exception.reason, "endpoint_unavailable")
        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertIn("incorrect header check", rendered)

    async def test_rejects_unknown_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_config(
                directory / "10-only.toml",
                reasoning_effort="turbo",
            )
            async with httpx.AsyncClient() as client:
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid interpreter reasoning effort",
                ):
                    _load_and_bind_endpoints(
                        directory,
                        http_client=client,
                        request_timeout_seconds=4,
                        turn_timeout_seconds=10,
                    )

    async def test_rejects_unusable_base_urls_while_loading_config(self) -> None:
        for base_url in (
            " https://models.example/v1",
            "https://models.example/v1\t",
            "https://models.exa\tmple/v1",
            "https://models.exa mple/v1",
            "https://models.example:bad/v1",
            "https://models.example:0/v1",
            "https://models.example:65536/v1",
            "https://models.exa\u00a0mple/v1",
            "https://models.exa\u200bmple/v1",
        ):
            with self.subTest(base_url=base_url):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    _write_config(directory / "10-only.toml", base_url=base_url)
                    async with httpx.AsyncClient() as client:
                        with self.assertRaisesRegex(
                            ValueError,
                            "invalid interpreter base URL",
                        ):
                            _load_and_bind_endpoints(
                                directory,
                                http_client=client,
                                request_timeout_seconds=4,
                                turn_timeout_seconds=10,
                            )

    async def test_rejects_invalid_configuration_id_and_model(self) -> None:
        for configuration_id, model in (("UPPER", "model"), ("only", " ")):
            with self.subTest(configuration_id=configuration_id, model=model):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    _write_config(
                        directory / "10-only.toml",
                        configuration_id=configuration_id,
                        model=model,
                    )
                    async with httpx.AsyncClient() as client:
                        with self.assertRaisesRegex(
                            ValueError,
                            "invalid interpreter endpoint identity",
                        ):
                            _load_and_bind_endpoints(
                                directory,
                                http_client=client,
                                request_timeout_seconds=4,
                                turn_timeout_seconds=10,
                            )

    async def test_rejects_non_regular_and_oversized_endpoint_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            path = directory / "10-only.toml"
            async with httpx.AsyncClient() as client:
                def assert_rejected() -> None:
                    with self.assertRaisesRegex(
                        ValueError,
                        "invalid interpreter endpoint configuration",
                    ):
                        _load_and_bind_endpoints(
                            directory,
                            http_client=client,
                            request_timeout_seconds=4,
                            turn_timeout_seconds=10,
                        )

                target = directory / "config-target"
                _write_config(target)
                path.symlink_to(target)
                assert_rejected()
                path.unlink()
                target.unlink()

                path.write_bytes(b"#" * (1024 * 1024))
                assert_rejected()

    async def test_invalid_toml_preserves_the_parser_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "10-only.toml").write_bytes(b"invalid = [")
            async with httpx.AsyncClient() as client:
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid interpreter endpoint configuration",
                ) as raised:
                    _load_and_bind_endpoints(
                        directory,
                        http_client=client,
                        request_timeout_seconds=4,
                        turn_timeout_seconds=10,
                    )

        self.assertIsNotNone(raised.exception.__cause__)
        self.assertEqual(type(raised.exception.__cause__).__name__, "TOMLDecodeError")

    async def test_maps_directory_snapshot_limits_to_honest_diagnostics(self) -> None:
        cases = (
            (
                LocalInputFailureReason.TOO_MANY_SELECTED_FILES,
                "at most 4 interpreter endpoints are supported",
            ),
            (
                LocalInputFailureReason.TOO_MANY_DIRECTORY_ENTRIES,
                "interpreter configuration directory has too many entries",
            ),
        )
        async with httpx.AsyncClient() as client:
            for reason, message in cases:
                with self.subTest(reason=reason.value), patch(
                    "nmrpeak_provider.openai_chat_interpreter."
                    "read_ordered_bounded_regular_files",
                    side_effect=LocalInputSnapshotError(reason),
                ):
                    with self.assertRaisesRegex(ValueError, f"^{message}$"):
                        _load_and_bind_endpoints(
                            Path("unused"),
                            http_client=client,
                            request_timeout_seconds=4,
                            turn_timeout_seconds=10,
                        )


def _load_and_bind_endpoints(
    directory: Path,
    *,
    http_client: httpx.AsyncClient,
    request_timeout_seconds: float,
    turn_timeout_seconds: float,
) -> OpenAIChatEndpoints:
    policy = OpenAIChatCallPolicy(
        request_timeout_seconds=request_timeout_seconds,
        turn_timeout_seconds=turn_timeout_seconds,
    )
    specs = load_openai_chat_endpoint_specs(directory)
    return bind_openai_chat_endpoints(specs, policy, http_client=http_client)


@asynccontextmanager
async def _loaded_endpoints(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
    *,
    request_timeout: float = 4,
    turn_timeout: float = 10,
) -> AsyncIterator[OpenAIChatEndpoints]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        _write_config(directory / "10-only.toml")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            endpoint_owner = _load_and_bind_endpoints(
                directory,
                http_client=client,
                request_timeout_seconds=request_timeout,
                turn_timeout_seconds=turn_timeout,
            )
            try:
                yield endpoint_owner
            finally:
                await endpoint_owner.join_response_releases()


class _HangingReadAndCloseStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.close_started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Future()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.close_started.set()
        await asyncio.Future()


class _FailingReadAndCloseStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise httpx.ReadError("private read failure")
        yield b"unreachable"

    async def aclose(self) -> None:
        raise RuntimeError("private close failure")


class _ControlledReadAndCloseStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_started.set()
        await asyncio.Future()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.close_started.set()
        try:
            await self.release_close.wait()
        finally:
            self.close_finished.set()


class _FailingReadControlledCloseStream(_ControlledReadAndCloseStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise httpx.ReadError("private read failure")
        yield b"unreachable"


class _FailsFirstCloseStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.close_calls = 0
        self.released = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._body

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("private close failure")
        self.released = True


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise RuntimeError("broken stream adapter")
        yield b"unreachable"


def _write_config(
    path: Path,
    *,
    configuration_id: str = "only",
    base_url: str = "http://127.0.0.1:8000/v1",
    api_key: str = "secret",
    model: str = "model",
    reasoning_effort: str | None = None,
) -> None:
    optional = (
        ()
        if reasoning_effort is None
        else (f'reasoning_effort = "{reasoning_effort}"',)
    )
    path.write_text(
        "\n".join(
            (
                f'id = "{configuration_id}"',
                f'base_url = "{base_url}"',
                f'api_key = "{api_key}"',
                f'model = "{model}"',
                *optional,
                "",
            )
        ),
        encoding="utf-8",
    )


def _completion(name: str, arguments: object) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": _completion_message(
                    name,
                    json.dumps(arguments),
                )
            }
        ]
    }


def _completion_message(
    name: str,
    arguments: str,
    *,
    call_id: str = "call-1",
) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
