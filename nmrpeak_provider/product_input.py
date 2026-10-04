"""Structured scientific input for the fixed HF and CHF offerings."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
from enum import StrEnum
import json
import re
from typing import Never

from .product import AnalysisOffering, NMRPEAK_PRODUCT


INPUT_SCHEMA_ID = "nmrpeak.structure_generation.request.v1"
MAX_JOB_INPUT_BYTES = 65_536

_FORMULA = re.compile(r"(?:(?:[A-Z][a-z]?|[+-])\d*)+")
SUPPORTED_MULTIPLICITIES = frozenset(
    {
        "AA'BB'",
        "AA'BB'C",
        'AB',
        'ABX',
        'ABq',
        'app_d',
        'app_dd',
        'app_dq',
        'app_dt',
        'app_q',
        'app_s',
        'app_t',
        'app_td',
        'br',
        'brd',
        'brdd',
        'brq',
        'brs',
        'brt',
        'd',
        'dd',
        'ddd',
        'dddd',
        'ddddd',
        'dddddd',
        'dddddt',
        'ddddq',
        'ddddt',
        'ddddtd',
        'dddp',
        'dddq',
        'dddqd',
        'dddt',
        'dddtd',
        'dddtt',
        'ddh',
        'ddp',
        'ddpd',
        'ddq',
        'ddqd',
        'ddqdd',
        'ddqt',
        'ddt',
        'ddtd',
        'ddtdd',
        'ddtdt',
        'ddtq',
        'ddtt',
        'ddttd',
        'dh',
        'dhd',
        'dhept',
        'dp',
        'dpd',
        'dpdd',
        'dpt',
        'dq',
        'dqd',
        'dqdd',
        'dqddd',
        'dqdt',
        'dqq',
        'dqt',
        'dqtd',
        'dt',
        'dtd',
        'dtdd',
        'dtddd',
        'dtddt',
        'dtdq',
        'dtdt',
        'dtdtd',
        'dtp',
        'dtq',
        'dtqd',
        'dtt',
        'dttd',
        'dttt',
        'h',
        'hd',
        'hdd',
        'hept',
        'heptd',
        'hex',
        'ht',
        'm',
        'p',
        'pd',
        'pdd',
        'pdt',
        'pq',
        'pt',
        'ptd',
        'q',
        'qd',
        'qdd',
        'qddd',
        'qddt',
        'qdq',
        'qdt',
        'qdtd',
        'qp',
        'qq',
        'qqd',
        'qt',
        'qtd',
        'qtdd',
        'qtt',
        's',
        'spt',
        't',
        'td',
        'tdd',
        'tddd',
        'tdddd',
        'tdddt',
        'tddq',
        'tddt',
        'tddtd',
        'tdp',
        'tdq',
        'tdqd',
        'tdt',
        'tdtd',
        'tdtdd',
        'tdtt',
        'th',
        'tp',
        'tpd',
        'tq',
        'tqd',
        'tqdd',
        'tqt',
        'tt',
        'ttd',
        'ttdd',
        'ttdt',
        'ttq',
        'ttt',
        'tttd',
    }
)


class InputRejectionReason(StrEnum):
    """Safe internal classification for a rejected scientific document."""

    DOCUMENT_TOO_LARGE = "document_too_large"
    EMPTY_INPUT = "empty_input"
    DISALLOWED_CONTROL = "disallowed_control"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_JSON = "invalid_json"
    DUPLICATE_FIELD = "duplicate_field"
    INVALID_STRUCTURE = "invalid_structure"
    WRONG_SPECTRA = "wrong_spectra"
    INVALID_FORMULA = "invalid_formula"
    UNSUPPORTED_MULTIPLICITY = "unsupported_multiplicity"
    COUPLING_MUST_BE_NONNEGATIVE = "coupling_must_be_nonnegative"


_OWNED_PATH_NAMES = frozenset({
    "schema_id", "model_input", "formula", "spectra", "1H", "13C", "peaks",
    "shift_lo", "shift_hi", "integral", "multiplicity", "j_hz", "shift",
})
_EXPECTED_FIXED = frozenset({
    f"at most {MAX_JOB_INPUT_BYTES} bytes",
    "the NMRPeak request schema identifier",
    "1H and 13C spectra", "1H spectrum",
    "JSON object", "JSON array", "a valid molecular formula",
    "at least one proton peak", "a whole-number integral from 1 to 50",
    "a supported proton multiplicity label", "at least one carbon peak",
    "a nonnegative coupling", "decimal text", "finite decimal text",
    "UTF-8 text without a byte-order mark",
    "printable UTF-8 text after JSON whitespace",
})
_FIELD_GROUPS = frozenset({
    frozenset({"schema_id", "model_input"}),
    frozenset({"formula", "spectra"}),
    frozenset({"peaks"}),
    frozenset({"shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"}),
    frozenset({"shift"}),
})
_RECEIVED_TYPES = frozenset({
    "JSON null", "JSON boolean", "JSON number", "JSON string",
    "JSON array", "JSON object", "a non-JSON value",
})
InputPath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class InputIssue:
    """A product-owned location and constraint, without rejected source text."""

    reason: InputRejectionReason
    path: InputPath = ()
    expected: str | None = None
    received_type: str | None = None
    line: int | None = None
    column: int | None = None

    def __post_init__(self) -> None:
        if type(self.reason) is not InputRejectionReason:
            raise ValueError("input issue reason must be product-owned")
        if type(self.path) is not tuple or len(self.path) > 16 or any(
            (type(segment) is str and segment not in _OWNED_PATH_NAMES)
            or (type(segment) is int and not 0 <= segment <= MAX_JOB_INPUT_BYTES)
            or type(segment) not in {str, int}
            for segment in self.path
        ):
            raise ValueError("input issue path must use product-owned fields and indices")
        if self.expected is not None and not _is_owned_expected(self.expected):
            raise ValueError("input issue expected shape must be product-owned")
        if self.received_type is not None and (
            type(self.received_type) is not str
            or self.received_type not in _RECEIVED_TYPES
        ):
            raise ValueError("input issue received type must be a JSON category")
        if (self.line is None) != (self.column is None) or any(
            type(value) is not int or not 1 <= value <= MAX_JOB_INPUT_BYTES + 1
            for value in (self.line, self.column) if value is not None
        ):
            raise ValueError("input issue location must be a bounded line and column")

    @property
    def pointer(self) -> str:
        return "".join(f"/{segment}" for segment in self.path)


def _is_owned_expected(value: object) -> bool:
    if type(value) is not str:
        return False
    if value in _EXPECTED_FIXED:
        return True
    parts = value.split("; ")
    if len(parts) > 2:
        return False
    required: frozenset[str] | None = None
    allowed: frozenset[str] | None = None
    for part in parts:
        if part.startswith("required fields ") and required is None:
            names = part.removeprefix("required fields ").split(", ")
            required = frozenset(names)
        elif part.startswith("allowed fields ") and allowed is None:
            names = part.removeprefix("allowed fields ").split(", ")
            allowed = frozenset(names)
        else:
            return False
        if not names or len(names) != len(set(names)) or names != sorted(names):
            return False
    for group in _FIELD_GROUPS:
        if allowed is not None and allowed != group:
            continue
        if required is not None and (not required or not required <= group):
            continue
        return True
    return False


class InputRejected(ValueError):
    """A Job document cannot enter one of this product's model lanes."""

    def __init__(self, reason: InputRejectionReason | InputIssue) -> None:
        self.issue = reason if type(reason) is InputIssue else InputIssue(reason)
        self.reason = self.issue.reason
        super().__init__(self.reason.value)


@dataclass(frozen=True, slots=True)
class ProtonPeak:
    """The exact NMRPeak values retained from one admitted proton peak."""

    centroid: Decimal
    integral: int
    multiplicity: str
    couplings_hz: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class CarbonPeak:
    """One admitted point-valued carbon observation."""

    shift: Decimal


@dataclass(frozen=True, slots=True)
class NmrpeakModelInput:
    """Scientific input shared by every admitted NMRPeak model lane."""

    formula: str
    proton_peaks: tuple[ProtonPeak, ...]


@dataclass(frozen=True, slots=True)
class HfModelInput(NmrpeakModelInput):
    """Parsed formula and proton input for the HF model lane."""


@dataclass(frozen=True, slots=True)
class ChfModelInput(NmrpeakModelInput):
    """Parsed formula, proton, and carbon input for the CHF model lane."""

    carbon_peaks: tuple[CarbonPeak, ...]


def parse_job_input(
    raw: bytes,
    offering: AnalysisOffering,
) -> HfModelInput | ChfModelInput:
    """Validate exact Job bytes for one statically selected product offering."""

    if not any(offering is admitted for admitted in NMRPEAK_PRODUCT.offerings):
        raise AssertionError("Job input parsing requires a product-owned offering")
    if type(raw) is not bytes:
        raise TypeError("Job input must be supplied as exact bytes")
    if len(raw) > MAX_JOB_INPUT_BYTES:
        _reject(
            InputRejectionReason.DOCUMENT_TOO_LARGE,
            expected=f"at most {MAX_JOB_INPUT_BYTES} bytes",
        )

    document = _decode_document(raw)
    request = _object_with_fields(document, {"schema_id", "model_input"}, ())
    if request["schema_id"] != INPUT_SCHEMA_ID:
        _reject(InputRejectionReason.INVALID_STRUCTURE, ("schema_id",),
                expected="the NMRPeak request schema identifier")
    model_path = ("model_input",)
    model_input = _object_with_fields(
        request["model_input"], {"formula", "spectra"}, model_path
    )
    formula = _parse_formula(model_input["formula"], model_path + ("formula",))
    spectra_path = model_path + ("spectra",)
    spectra = _object(model_input["spectra"], spectra_path)

    requires_carbon = offering.implementation_ref == "chf"
    expected_nuclei = {"1H", "13C"} if requires_carbon else {"1H"}
    if set(spectra) != expected_nuclei:
        _reject(
            InputRejectionReason.WRONG_SPECTRA, spectra_path,
            expected="1H and 13C spectra" if requires_carbon else "1H spectrum",
        )

    proton_peaks = _parse_proton_spectrum(
        spectra["1H"], spectra_path + ("1H",)
    )
    if requires_carbon:
        return ChfModelInput(
            formula=formula,
            proton_peaks=proton_peaks,
            carbon_peaks=_parse_carbon_spectrum(
                spectra["13C"], spectra_path + ("13C",)
            ),
        )
    return HfModelInput(formula=formula, proton_peaks=proton_peaks)


def _decode_document(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except _DuplicateField as error:
        raise InputRejected(InputRejectionReason.DUPLICATE_FIELD) from error
    except UnicodeDecodeError as error:
        raise InputRejected(InputRejectionReason.INVALID_UTF8) from error
    except json.JSONDecodeError as error:
        raise InputRejected(InputIssue(
            InputRejectionReason.INVALID_JSON,
            line=error.lineno,
            column=error.colno,
        )) from error
    except (_InvalidJsonNumber, RecursionError, ValueError) as error:
        raise InputRejected(InputRejectionReason.INVALID_JSON) from error


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateField
        value[key] = item
    return value


def _reject_json_number(_value: str) -> Never:
    raise _InvalidJsonNumber


class _DuplicateField(ValueError):
    pass


class _InvalidJsonNumber(ValueError):
    pass


def _json_type(value: object) -> str:
    return {
        type(None): "JSON null", bool: "JSON boolean", int: "JSON number",
        float: "JSON number", str: "JSON string", list: "JSON array",
        dict: "JSON object",
    }.get(type(value), "a non-JSON value")


def _object(value: object, path: InputPath) -> dict[str, object]:
    if type(value) is not dict:
        _reject(
            InputRejectionReason.INVALID_STRUCTURE, path,
            expected="JSON object", received_type=_json_type(value),
        )
    return value


def _object_with_fields(
    value: object, fields: set[str], path: InputPath
) -> dict[str, object]:
    object_value = _object(value, path)
    observed = set(object_value)
    missing = fields - observed
    extra = observed - fields
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append("required fields " + ", ".join(sorted(missing)))
        if extra:
            parts.append("allowed fields " + ", ".join(sorted(fields)))
        _reject(
            InputRejectionReason.INVALID_STRUCTURE, path,
            expected="; ".join(parts),
        )
    return object_value


def _array(value: object, path: InputPath) -> list[object]:
    if type(value) is not list:
        _reject(
            InputRejectionReason.INVALID_STRUCTURE, path,
            expected="JSON array", received_type=_json_type(value),
        )
    return value


def _parse_formula(value: object, path: InputPath) -> str:
    if type(value) is not str or _FORMULA.fullmatch(value) is None:
        _reject(InputRejectionReason.INVALID_FORMULA, path,
                expected="a valid molecular formula")
    return value


def _parse_proton_spectrum(
    value: object, path: InputPath
) -> tuple[ProtonPeak, ...]:
    spectrum = _object_with_fields(value, {"peaks"}, path)
    peaks_path = path + ("peaks",)
    peaks = _array(spectrum["peaks"], peaks_path)
    if not peaks:
        _reject(InputRejectionReason.INVALID_STRUCTURE, peaks_path,
                expected="at least one proton peak")
    parsed = tuple(
        _parse_proton_peak(peak, peaks_path + (index,))
        for index, peak in enumerate(peaks)
    )
    return tuple(sorted(parsed, key=lambda peak: peak.centroid, reverse=True))


def _parse_proton_peak(value: object, path: InputPath) -> ProtonPeak:
    peak = _object_with_fields(
        value,
        {"shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"},
        path,
    )
    first_shift = _decimal(peak["shift_lo"], path + ("shift_lo",))
    second_shift = _decimal(peak["shift_hi"], path + ("shift_hi",))
    shift_lo, shift_hi = sorted((first_shift, second_shift))
    centroid = (shift_lo + shift_hi) / 2

    integral = peak["integral"]
    if (
        type(integral) is not str
        or re.fullmatch(r"[1-9][0-9]?", integral) is None
        or int(integral) > 50
    ):
        _reject(InputRejectionReason.INVALID_STRUCTURE, path + ("integral",),
                expected="a whole-number integral from 1 to 50")
    multiplicity = peak["multiplicity"]
    if type(multiplicity) is not str or multiplicity not in SUPPORTED_MULTIPLICITIES:
        _reject(
            InputRejectionReason.UNSUPPORTED_MULTIPLICITY,
            path + ("multiplicity",),
            expected="a supported proton multiplicity label",
            received_type=_json_type(multiplicity),
        )

    couplings_path = path + ("j_hz",)
    raw_couplings = _array(peak["j_hz"], couplings_path)
    couplings = tuple(
        _parse_coupling(coupling, couplings_path + (index,))
        for index, coupling in enumerate(raw_couplings)
    )
    return ProtonPeak(
        centroid=centroid,
        integral=int(integral),
        multiplicity=multiplicity,
        couplings_hz=tuple(sorted(couplings, reverse=True)),
    )


def _parse_carbon_spectrum(
    value: object, path: InputPath
) -> tuple[CarbonPeak, ...]:
    spectrum = _object_with_fields(value, {"peaks"}, path)
    peaks_path = path + ("peaks",)
    peaks = _array(spectrum["peaks"], peaks_path)
    if not peaks:
        _reject(InputRejectionReason.INVALID_STRUCTURE, peaks_path,
                expected="at least one carbon peak")
    parsed = tuple(
        _parse_carbon_peak(peak, peaks_path + (index,))
        for index, peak in enumerate(peaks)
    )
    return tuple(sorted(parsed, key=lambda peak: peak.shift, reverse=True))


def _parse_carbon_peak(value: object, path: InputPath) -> CarbonPeak:
    peak = _object_with_fields(value, {"shift"}, path)
    return CarbonPeak(shift=_decimal(peak["shift"], path + ("shift",)))


def _parse_coupling(value: object, path: InputPath) -> Decimal:
    coupling = _decimal(value, path)
    if coupling < 0:
        _reject(InputRejectionReason.COUPLING_MUST_BE_NONNEGATIVE, path,
                expected="a nonnegative coupling")
    return coupling


def _decimal(
    value: object,
    path: InputPath,
) -> Decimal:
    if type(value) is not str:
        _reject(InputRejectionReason.INVALID_STRUCTURE, path,
                expected="decimal text", received_type=_json_type(value))
    try:
        parsed = Decimal(value)
    except DecimalException as error:
        raise InputRejected(InputIssue(
            InputRejectionReason.INVALID_STRUCTURE, path,
            expected="decimal text",
        )) from error
    if not parsed.is_finite():
        _reject(InputRejectionReason.INVALID_STRUCTURE, path,
                expected="finite decimal text")
    return parsed


def _reject(
    reason: InputRejectionReason,
    path: InputPath = (),
    *,
    expected: str | None = None,
    received_type: str | None = None,
) -> Never:
    raise InputRejected(InputIssue(
        reason, path, expected=expected, received_type=received_type
    ))
