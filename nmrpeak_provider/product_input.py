"""Strict scientific input admitted by the fixed HF and CHF offerings."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
import json
import re
from typing import Never

from .product import AnalysisOffering, NMRPEAK_PRODUCT


INPUT_SCHEMA_ID = "nmrpeak.structure_generation.request.v1"
MAX_JOB_INPUT_BYTES = 65_536
MAX_PROTON_PEAKS = 32
MAX_CARBON_PEAKS = 64
MAX_COUPLINGS_PER_PEAK = 8
MAX_FORMULA_ATOMS = 100

_FORMULA_TOKEN = re.compile(r"([A-Z][a-z]?)([1-9][0-9]{0,2})?")
_PROTON_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?")
_CARBON_OR_COUPLING_DECIMAL = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9])?"
)
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
    DECIMAL_OUT_OF_RANGE = "decimal_out_of_range"
    MIDPOINT_NOT_REPRESENTABLE = "midpoint_not_representable"
    UNSUPPORTED_MULTIPLICITY = "unsupported_multiplicity"
    TOO_MANY_PEAKS = "too_many_peaks"
    TOO_MANY_COUPLINGS = "too_many_couplings"


class InputRejected(ValueError):
    """A Job document cannot enter one of this product's model lanes."""

    public_message = (
        "This analysis could not accept the supplied input, so the Job did not run. "
        "Review the analysis's input requirements. Submit a new Job only if you can "
        "provide input that meets them."
    )

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
    """Validated formula and proton input for the HF model lane."""


@dataclass(frozen=True, slots=True)
class ChfModelInput(NmrpeakModelInput):
    """Validated formula, proton, and carbon input for the CHF model lane."""

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
    except _DuplicateField:
        _reject(InputRejectionReason.DUPLICATE_FIELD)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _InvalidJsonNumber,
        RecursionError,
        ValueError,
    ):
        _reject(InputRejectionReason.INVALID_JSON)


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
    if type(value) is not str or not value:
        _reject(InputRejectionReason.INVALID_FORMULA)
    position = 0
    composition: dict[str, int] = {}
    while position < len(value):
        match = _FORMULA_TOKEN.match(value, position)
        if match is None:
            _reject(InputRejectionReason.INVALID_FORMULA)
        element, raw_count = match.groups()
        if element in composition:
            _reject(InputRejectionReason.INVALID_FORMULA)
        count = int(raw_count) if raw_count is not None else 1
        if count > MAX_FORMULA_ATOMS:
            _reject(InputRejectionReason.INVALID_FORMULA)
        composition[element] = count
        position = match.end()
    if sum(composition.values()) > MAX_FORMULA_ATOMS:
        _reject(InputRejectionReason.INVALID_FORMULA)
    if "C" in composition:
        order = ["C"]
        if "H" in composition:
            order.append("H")
        order.extend(sorted(set(composition) - {"C", "H"}))
    else:
        order = ["H"] if "H" in composition else []
        order.extend(sorted(set(composition) - {"H"}))
    return "".join(
        element + (str(composition[element]) if composition[element] != 1 else "")
        for element in order
    )


def _parse_proton_spectrum(value: object) -> tuple[ProtonPeak, ...]:
    spectrum = _object_with_fields(value, {"peaks"})
    peaks = _array(spectrum["peaks"])
    if not peaks:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    if len(peaks) > MAX_PROTON_PEAKS:
        _reject(InputRejectionReason.TOO_MANY_PEAKS)
    parsed = tuple(_parse_proton_peak(peak) for peak in peaks)
    return tuple(sorted(parsed, key=lambda peak: peak.centroid, reverse=True))


def _parse_proton_peak(value: object) -> ProtonPeak:
    peak = _object_with_fields(
        value,
        {"shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"},
    )
    shift_lo = _decimal(peak["shift_lo"], _PROTON_DECIMAL, Decimal("-1"), Decimal("16"))
    shift_hi = _decimal(peak["shift_hi"], _PROTON_DECIMAL, Decimal("-1"), Decimal("16"))
    if shift_lo > shift_hi:
        _reject(InputRejectionReason.DECIMAL_OUT_OF_RANGE)
    centroid = (shift_lo + shift_hi) / 2
    if centroid != centroid.quantize(Decimal("0.01")):
        _reject(InputRejectionReason.MIDPOINT_NOT_REPRESENTABLE)

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
    if len(raw_couplings) > MAX_COUPLINGS_PER_PEAK:
        _reject(InputRejectionReason.TOO_MANY_COUPLINGS)
    couplings = tuple(
        _decimal(
            coupling,
            _CARBON_OR_COUPLING_DECIMAL,
            Decimal("0.1"),
            Decimal("300"),
        )
        for coupling in raw_couplings
    )
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
    if len(peaks) > MAX_CARBON_PEAKS:
        _reject(InputRejectionReason.TOO_MANY_PEAKS)
    parsed = tuple(_parse_carbon_peak(peak) for peak in peaks)
    return tuple(sorted(parsed, key=lambda peak: peak.shift, reverse=True))


def _parse_carbon_peak(value: object) -> CarbonPeak:
    peak = _object_with_fields(value, {"shift"})
    return CarbonPeak(
        shift=_decimal(
            peak["shift"],
            _CARBON_OR_COUPLING_DECIMAL,
            Decimal("-6"),
            Decimal("250"),
        )
    )


def _decimal(
    value: object,
    grammar: re.Pattern[str],
    lower_bound: Decimal,
    excluded_upper_bound: Decimal,
) -> Decimal:
    if type(value) is not str or grammar.fullmatch(value) is None:
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    parsed = Decimal(value)
    if parsed == 0 and value.startswith("-"):
        _reject(InputRejectionReason.INVALID_STRUCTURE)
    if parsed < lower_bound or parsed >= excluded_upper_bound:
        _reject(InputRejectionReason.DECIMAL_OUT_OF_RANGE)
    return parsed


def _reject(reason: InputRejectionReason) -> Never:
    raise InputRejected(reason) from None
