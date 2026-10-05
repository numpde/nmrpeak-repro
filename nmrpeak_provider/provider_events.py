"""Closed, bounded scalar events for provider operator logs.

The renderer and catalogue structure are copied and narrowed from
magnet-deploy's provider_events module. NMRPeak owns the event names and facts
below; arbitrary objects and unbounded text never cross this boundary.
"""

from dataclasses import MISSING, dataclass, field, fields
from functools import cache
import re
from types import MappingProxyType, UnionType
from typing import ClassVar, get_args, get_origin


MAX_PROVIDER_EVENT_BYTES = 16 * 1024
_MAX_TEXT_BYTES = 4 * 1024
_MAX_TUPLE_ITEMS = 16
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_EVENT_LABEL = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_LABEL = "provider_event_label"
_OMIT_NONE = "provider_event_omit_none"


class ProviderEventError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"provider event rejected: {reason}")


def _event_field(
    *, label: str | None = None, omit_if_none: bool = False, default: object = MISSING
) -> object:
    metadata = MappingProxyType({_LABEL: label, _OMIT_NONE: omit_if_none})
    if default is MISSING:
        return field(metadata=metadata)
    return field(default=default, metadata=metadata)


def _bounded_text(value: object) -> str:
    if type(value) is not str:
        raise ProviderEventError("fact_value")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ProviderEventError("fact_value") from None
    if size > _MAX_TEXT_BYTES:
        raise ProviderEventError("fact_text_too_large")
    return repr(value)


@cache
def _fact_renderer(annotation: object):
    if annotation is str:
        return _bounded_text
    if annotation is int:
        def render_integer(value: object) -> str:
            if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
                raise ProviderEventError("fact_value")
            return str(value)
        return render_integer
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is tuple and arguments == (str, Ellipsis):
        def render_tuple(value: object) -> str:
            if type(value) is not tuple or len(value) > _MAX_TUPLE_ITEMS:
                raise ProviderEventError("fact_value")
            if any(type(item) is not str for item in value):
                raise ProviderEventError("fact_value")
            try:
                size = sum(len(item.encode("utf-8")) for item in value)
            except UnicodeError:
                raise ProviderEventError("fact_value") from None
            if size > _MAX_TEXT_BYTES:
                raise ProviderEventError("fact_text_too_large")
            return repr(value)
        return render_tuple
    if origin is UnionType and len(arguments) == 2 and type(None) in arguments:
        required = next(argument for argument in arguments if argument is not type(None))
        renderer = _fact_renderer(required)
        return lambda value: "None" if value is None else renderer(value)
    raise ProviderEventError("fact_annotation")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderEvent:
    EVENT_FIELD: ClassVar[str] = "provider_event"
    EVENT_CODE: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        if kwargs or cls.__module__ != __name__:
            raise TypeError("provider event classes are sealed")

    def __post_init__(self) -> None:
        if type(self) is ProviderEvent:
            raise ProviderEventError("event_type")
        _render_event_message(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterEndpointFailed(ProviderEvent):
    EVENT_FIELD = "interpreter_event"
    EVENT_CODE = "endpoint_failed"

    configuration_id: str
    failure_kind: str
    failure_reason: str
    execution_attempt_ref: str
    failure_state: str | None = _event_field(omit_if_none=True, default=None)
    http_status: int | None = _event_field(omit_if_none=True, default=None)
    error_type: str | None = _event_field(omit_if_none=True, default=None)
    error_code: str | None = _event_field(omit_if_none=True, default=None)
    request_id: str | None = _event_field(omit_if_none=True, default=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterRoute(ProviderEvent):
    EVENT_FIELD = "interpreter_event"
    EVENT_CODE = "route"

    execution_attempt_ref: str
    disposition: str
    configuration_id: str | None
    attempted_configuration_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationFailurePolicyDrift(ProviderEvent):
    EVENT_CODE = "preparation_failure_policy_drift"

    job_ref: str
    execution_attempt_ref: str
    failure_kind: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationFailureRetained(ProviderEvent):
    EVENT_CODE = "preparation_failure_retained"

    job_ref: str
    execution_attempt_ref: str
    failure_kind: str
    failure_code: str
    reason: str
    path: str
    endpoint_route: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptConditionConfirmed(ProviderEvent):
    EVENT_CODE = "attempt_condition_confirmed"

    job_ref: str
    execution_attempt_ref: str
    context: str
    condition_code: str | None
    updated_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptConditionUnconfirmed(ProviderEvent):
    EVENT_CODE = "attempt_condition_unconfirmed"

    job_ref: str
    execution_attempt_ref: str
    context: str
    condition_code: str | None
    outcome_type: str
    evidence_type: str
    http_status: int | None = _event_field(omit_if_none=True, default=None)
    reason: str | None = _event_field(omit_if_none=True, default=None)
    code: str | None = _event_field(omit_if_none=True, default=None)
    delivery: str | None = _event_field(omit_if_none=True, default=None)
    problem_type: str | None = _event_field(omit_if_none=True, default=None)
    problem_title: str | None = _event_field(omit_if_none=True, default=None)
    transport_request_id: str | None = _event_field(omit_if_none=True, default=None)
    body_request_id: str | None = _event_field(omit_if_none=True, default=None)


PROVIDER_EVENT_TYPES: tuple[type[ProviderEvent], ...] = (
    InterpreterEndpointFailed,
    InterpreterRoute,
    PreparationFailurePolicyDrift,
    PreparationFailureRetained,
    AttemptConditionConfirmed,
    AttemptConditionUnconfirmed,
)


def require_provider_event(value: object) -> ProviderEvent:
    if type(value) not in PROVIDER_EVENT_TYPES:
        raise ProviderEventError("event_type")
    return value


def _validate_event_class(event_type: type[ProviderEvent]) -> None:
    if event_type.EVENT_FIELD not in {"provider_event", "interpreter_event"}:
        raise ProviderEventError("event_field")
    if type(event_type.EVENT_CODE) is not str or _EVENT_NAME.fullmatch(event_type.EVENT_CODE) is None:
        raise ProviderEventError("event_code")
    labels = set()
    if "__post_init__" in event_type.__dict__:
        raise ProviderEventError("event_post_init")
    for fact in fields(event_type):
        _fact_renderer(fact.type)
        label = fact.metadata.get(_LABEL) or fact.name
        if type(label) is not str or _EVENT_LABEL.fullmatch(label) is None or label in labels:
            raise ProviderEventError("fact_label")
        labels.add(label)
        omit_none = fact.metadata.get(_OMIT_NONE, False)
        if type(omit_none) is not bool:
            raise ProviderEventError("omit_if_none")
        if omit_none and not (
            fact.default is None
            and get_origin(fact.type) is UnionType
            and type(None) in get_args(fact.type)
        ):
            raise ProviderEventError("omitted_fact_contract")


def _render_event_message(event: ProviderEvent) -> str:
    _validate_event_class(type(event))
    message = f"{event.EVENT_FIELD}={event.EVENT_CODE!r}"
    for fact in fields(event):
        value = getattr(event, fact.name)
        if fact.metadata.get(_OMIT_NONE, False) and value is None:
            continue
        label = fact.metadata.get(_LABEL) or fact.name
        message += f"; {label}={_fact_renderer(fact.type)(value)}"
    if len(message.encode("utf-8")) > MAX_PROVIDER_EVENT_BYTES:
        raise ProviderEventError("event_too_large")
    return message


def render_provider_event(event: ProviderEvent) -> str:
    return _render_event_message(require_provider_event(event))


def _validate_catalogue() -> None:
    declared = {
        value for value in globals().values()
        if type(value) is type
        and value is not ProviderEvent
        and issubclass(value, ProviderEvent)
        and value.__module__ == __name__
    }
    if len(set(PROVIDER_EVENT_TYPES)) != len(PROVIDER_EVENT_TYPES):
        raise ProviderEventError("duplicate_event_class")
    if declared != set(PROVIDER_EVENT_TYPES):
        raise ProviderEventError("event_catalogue")
    identities = {
        (event_type.EVENT_FIELD, event_type.EVENT_CODE)
        for event_type in PROVIDER_EVENT_TYPES
    }
    if len(identities) != len(PROVIDER_EVENT_TYPES):
        raise ProviderEventError("duplicate_event_identity")
    for event_type in PROVIDER_EVENT_TYPES:
        if "EVENT_CODE" not in event_type.__dict__:
            raise ProviderEventError("event_code")
        _validate_event_class(event_type)


_validate_catalogue()
