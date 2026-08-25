Interpret the source as a molecular-structure request from a molecular
formula and unassigned proton and carbon-13 NMR peak lists.

`submit_interpretation.value` must be the complete JSON object below, populated
only from the source:

```json
{
  "schema_id": "nmrpeak.structure_generation.request.v1",
  "model_input": {
    "formula": "C2H6O",
    "spectra": {
      "1H": {
        "peaks": [
          {
            "shift_lo": "1.20",
            "shift_hi": "1.30",
            "integral": "3",
            "multiplicity": "t",
            "j_hz": ["7.1"]
          }
        ]
      },
      "13C": {"peaks": [{"shift": "58.1"}]}
    }
  }
}
```

Copy the complete molecular formula exactly as reported. Do not reorder,
balance, complete, correct, or assess it. The application owns formula syntax,
canonicalization, bounds, and model-input validation.

Both spectra are required and each contains at least one peak. Each proton peak
has exactly the five fields shown. Preserve reported shift bounds; use the same
reported shift for both bounds when it is a point value. Copy proton shifts,
integrals, couplings, and carbon shifts into the shown string fields without
rounding. `j_hz` is always a list and may be empty. Multiplicity must preserve
the reported NMR label, such as `s`, `d`, `t`, `q`, `m`, `dd`, `dt`, or `brs`.

Do not infer or repair missing values. Ignore candidate structures, identifiers,
provenance, evaluation, and decode metadata. Call `report_input_problem` only
when the formula or either usable peak list is missing, ambiguous, contradictory
in the source, or cannot be transcribed without inventing a required value. Do
not report a problem merely because supplied values appear chemically unusual
or mutually implausible.
