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
    INVALID_JSON = "invalid_json"
    DUPLICATE_FIELD = "duplicate_field"
    INVALID_STRUCTURE = "invalid_structure"
    WRONG_SPECTRA = "wrong_spectra"
    INVALID_FORMULA = "invalid_formula"
    UNSUPPORTED_MULTIPLICITY = "unsupported_multiplicity"
    COUPLING_MUST_BE_NONNEGATIVE = "coupling_must_be_nonnegative"


class InputRejected(ValueError):
    """A Job document cannot enter one of this product's model lanes."""

    def __init__(self, reason: InputRejectionReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


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
        _reject(InputRejectionReason.DOCUMENT_TOO_LARGE)

    document = _decode_document(raw)
    request = _object_with_fields(document, {"schema_id", "model_input"})
    if request["schema_id"] != INPUT_SCHEMA_ID:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    model_input = _object_with_fields(request["model_input"], {"formula", "spectra"})
    formula = _parse_formula(model_input["formula"])
    spectra = _object(model_input["spectra"])

    requires_carbon = offering.implementation_ref == "chf"
    expected_nuclei = {"1H", "13C"} if requires_carbon else {"1H"}
    if set(spectra) != expected_nuclei:
        _reject(InputRejectionReason.WRONG_SPECTRA)

    proton_peaks = _parse_proton_spectrum(spectra["1H"])
    if requires_carbon:
        return ChfModelInput(
            formula=formula,
            proton_peaks=proton_peaks,
            carbon_peaks=_parse_carbon_spectrum(spectra["13C"]),
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
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _InvalidJsonNumber,
        RecursionError,
        ValueError,
    ) as error:
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


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    return value


def _object_with_fields(value: object, fields: set[str]) -> dict[str, object]:
    object_value = _object(value)
    if set(object_value) != fields:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    return object_value


def _array(value: object) -> list[object]:
    if type(value) is not list:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    return value


def _parse_formula(value: object) -> str:
    if type(value) is not str or _FORMULA.fullmatch(value) is None:
        _reject(InputRejectionReason.INVALID_FORMULA)
    return value


def _parse_proton_spectrum(value: object) -> tuple[ProtonPeak, ...]:
    spectrum = _object_with_fields(value, {"peaks"})
    peaks = _array(spectrum["peaks"])
    if not peaks:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    parsed = tuple(_parse_proton_peak(peak) for peak in peaks)
    return tuple(sorted(parsed, key=lambda peak: peak.centroid, reverse=True))


def _parse_proton_peak(value: object) -> ProtonPeak:
    peak = _object_with_fields(
        value,
        {"shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"},
    )
    first_shift = _decimal(peak["shift_lo"])
    second_shift = _decimal(peak["shift_hi"])
    shift_lo, shift_hi = sorted((first_shift, second_shift))
    centroid = (shift_lo + shift_hi) / 2

    integral = peak["integral"]
    if (
        type(integral) is not str
        or re.fullmatch(r"[1-9][0-9]?", integral) is None
        or int(integral) > 50
    ):
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    multiplicity = peak["multiplicity"]
    if type(multiplicity) is not str or multiplicity not in SUPPORTED_MULTIPLICITIES:
        _reject(InputRejectionReason.UNSUPPORTED_MULTIPLICITY)

    raw_couplings = _array(peak["j_hz"])
    couplings = tuple(_parse_coupling(coupling) for coupling in raw_couplings)
    return ProtonPeak(
        centroid=centroid,
        integral=int(integral),
        multiplicity=multiplicity,
        couplings_hz=tuple(sorted(couplings, reverse=True)),
    )


def _parse_carbon_spectrum(value: object) -> tuple[CarbonPeak, ...]:
    spectrum = _object_with_fields(value, {"peaks"})
    peaks = _array(spectrum["peaks"])
    if not peaks:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    parsed = tuple(_parse_carbon_peak(peak) for peak in peaks)
    return tuple(sorted(parsed, key=lambda peak: peak.shift, reverse=True))


def _parse_carbon_peak(value: object) -> CarbonPeak:
    peak = _object_with_fields(value, {"shift"})
    return CarbonPeak(shift=_decimal(peak["shift"]))


def _parse_coupling(value: object) -> Decimal:
    coupling = _decimal(value)
    if coupling < 0:
        _reject(InputRejectionReason.COUPLING_MUST_BE_NONNEGATIVE)
    return coupling


def _decimal(
    value: object,
) -> Decimal:
    if type(value) is not str:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    try:
        parsed = Decimal(value)
    except DecimalException as error:
        raise InputRejected(InputRejectionReason.INVALID_STRUCTURE) from error
    if not parsed.is_finite():
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    return parsed


def _reject(reason: InputRejectionReason) -> Never:
    raise InputRejected(reason)
