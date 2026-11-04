"""Model-message fixtures for the shared interpreter's endpoint factory contract."""

from copy import deepcopy
from itertools import count
import json
from types import SimpleNamespace

_call_ids = count(1)


def turn(name, arguments, *, call_id=None):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id or f"call-{next(_call_ids)}", "type": "function",
        "function": {"name": str(name), "arguments": json.dumps(arguments)},
    }]}


class FakeEndpoint:
    def __init__(self, configuration_id, call):
        self._call = call
        self._active = False
        self.identity = json.dumps({"configuration_id": configuration_id, "model": "fake",
                                    "configuration_sha256": "a" * 64}).encode("ascii")

    @property
    def retirement_confirmed(self):
        return not self._active

    async def complete(self, messages, tools, *, not_after):
        self._active = True
        try:
            return await self._call(deepcopy(messages))
        finally:
            self._active = False

    async def retire(self):
        if self._active:
            raise RuntimeError("Fake endpoint still has an active call")


class Factory:
    context_bytes = 1024 * 1024

    def __init__(self, carrier):
        self.carrier = carrier

    def __call__(self, **grant):
        return self.carrier


def fake_endpoint(configuration_id, call, *, lane, secrets=()):
    carrier = FakeEndpoint(configuration_id, call)
    return SimpleNamespace(configuration_id=configuration_id, api_key=secrets[0] if secrets else "fixture-key",
                           factory=Factory(carrier), pace=None)
