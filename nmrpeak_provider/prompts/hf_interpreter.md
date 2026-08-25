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

The formula must be neutral and use valid element symbols with positive integer
counts. Use NMRPeak's formula order: C first when present, H next when present,
then every other element alphabetically. Without C, H remains first when
present. Each proton peak has exactly the five fields shown. Preserve reported
shift bounds; use the same reported shift for both bounds when it is a point
value. Shifts use at most two decimal places. Integrals are positive integer
strings from 1 through 50. Couplings are strings from 0.1 through 299.9 with at
most one decimal place. `j_hz` is always a list and may be empty. Multiplicity
must preserve the reported NMR label, such as `s`, `d`, `t`, `q`, `m`, `dd`,
`dt`, or `brs`.

At least one proton peak is required. Do not infer, round, or repair missing
scientific values. Ignore candidate structures, identifiers, provenance,
evaluation, and decode metadata. If the formula or usable proton peak list
cannot be determined reliably, call `report_input_problem` with a helpful
explanation of what the caller must provide or clarify.
