"""Raw model-message fixtures with explicitly owned fake endpoint lifetimes."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
from itertools import count
import json

from nmrpeak_provider.input_interpreter import (
    _REPORT_DESCRIPTION, _SUBMIT_DESCRIPTION, _value_schema_for,
)
from nmrpeak_provider.interpreter import InterpreterEndpoint, InterpreterPrompt, InterpreterTool
from nmrpeak_provider.openai_chat_interpreter import _interpreter_tools

InterpreterCall = Callable[[InterpreterPrompt], Awaitable[dict[str, object]]]
_call_ids = count(1)


def turn(name: InterpreterTool | str, arguments: object, *, call_id: str | None = None):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": call_id if call_id is not None else f"call-{next(_call_ids)}",
            "type": "function",
            "function": {"name": str(name), "arguments": json.dumps(arguments)},
        }],
    }


class FakeEndpoint:
    def __init__(self, configuration_id, call):
        self._call = call
        self._closed = False
        self._active = False
        self.identity = json.dumps({"configuration_id": configuration_id}).encode("ascii")

    @property
    def retirement_confirmed(self):
        return not self._active

    async def complete(self, messages, tools):
        if self._closed:
            raise RuntimeError("Fake endpoint has retired")
        self._active = True
        try:
            # Captured prompts represent the actual turn boundary, even though
            # the SDK continues mutating its privately owned transcript.
            return await self._call(deepcopy(messages))
        finally:
            self._active = False

    async def retire(self):
        self._closed = True
        if self._active:
            raise RuntimeError("Fake endpoint still has an active call")


def fake_endpoint(configuration_id, call, *, lane, secrets=()):
    tools = _interpreter_tools(
        submit_interpretation_description=_SUBMIT_DESCRIPTION,
        submit_interpretation_value_schema=_value_schema_for(lane),
        report_input_problem_description=_REPORT_DESCRIPTION,
    )
    return InterpreterEndpoint(configuration_id, FakeEndpoint(configuration_id, call), tools, secrets)
