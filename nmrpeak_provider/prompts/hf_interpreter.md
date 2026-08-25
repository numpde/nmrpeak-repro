Interpret the source as a molecular-structure request from a molecular
formula and an unassigned proton NMR peak list.

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
      }
    }
  }
}
```

Copy the complete molecular formula exactly as reported. Do not reorder,
balance, complete, correct, or assess it. The application owns formula syntax,
canonicalization, bounds, and model-input validation.

Each proton peak has exactly the five fields shown. Preserve reported shift
bounds; use the same reported shift for both bounds when it is a point value.
Copy shifts, integrals, and couplings into the shown string fields without
rounding. `j_hz` is always a list and may be empty. Multiplicity must preserve
the reported NMR label, such as `s`, `d`, `t`, `q`, `m`, `dd`, `dt`, or `brs`.

At least one proton peak is required. If a required peak field is not reported,
do not infer it. Ignore candidate structures, identifiers, provenance,
evaluation, and decode metadata. A reported formula or peak value remains
source data even when it appears chemically unusual. Do not cross-check the
formula and peaks for scientific consistency.
