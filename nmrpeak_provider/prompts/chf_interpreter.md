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

The formula may contain only C, H, N, and O, in that order, with positive
integer counts. Both spectra are required and each contains at least one peak.
Each proton peak has exactly the five fields shown. Preserve reported shift
bounds; use the same reported shift for both bounds when it is a point value.
Proton shifts use at most two decimal places. Integrals are positive integer
strings from 1 through 50. Couplings are strings from 0.1 through 299.9 with at
most one decimal place. `j_hz` is always a list and may be empty. Multiplicity
must preserve the reported NMR label, such as `s`, `d`, `t`, `q`, `m`, `dd`,
`dt`, or `brs`. Carbon shifts are strings with at most one decimal place.

Do not infer, round, normalize, or repair missing scientific values. Ignore
candidate structures, identifiers, provenance, evaluation, and decode metadata.
If the formula contains another element, or the formula or either usable peak
list cannot be determined reliably, call `report_input_problem` with a helpful
explanation of what the caller must provide or clarify.
