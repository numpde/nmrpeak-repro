"""Source-faithful examples shared by scientific session tests."""

SOURCE = b"Formula C2H6O. 1H: 1.25 (t, 3H, J 7.1 Hz). 13C: 58.1."
VALUE = {
    "schema_id": "nmrpeak.structure_generation.request.v1",
    "model_input": {
        "formula": "C2H6O",
        "spectra": {
            "1H": {
                "peaks": [
                    {
                        "shift_lo": "1.25",
                        "shift_hi": "1.25",
                        "integral": "3",
                        "multiplicity": "t",
                        "j_hz": ["7.1"],
                    }
                ]
            },
            "13C": {"peaks": [{"shift": "58.1"}]},
        },
    },
}
HF_VALUE = {
    "schema_id": "nmrpeak.structure_generation.request.v1",
    "model_input": {
        "formula": "C2H6O",
        "spectra": {"1H": VALUE["model_input"]["spectra"]["1H"]},
    },
}

