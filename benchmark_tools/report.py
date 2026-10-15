"""Derive summaries from retained benchmark events, including incomplete runs."""
from __future__ import annotations

import json
import math
from pathlib import Path
import statistics


def percentile95(values):
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def compare_outputs(reference, candidate):
    left, right = set(reference["canonical_smiles"]) - {None}, set(candidate["canonical_smiles"]) - {None}
    union = left | right
    return {"ranked_equal": reference["candidates"] == candidate["candidates"],
            "molecule_overlap": len(left & right) / len(union) if union else None}


def write_report(directory: Path, manifest: dict) -> dict:
    measurements, outputs, summaries = [], [], []
    for run in manifest["runs"]:
        events = []
        run["event_errors"] = []
        stream = directory / "logs" / f'{run["id"]}.jsonl'
        if stream.exists():
            lines = stream.read_text().splitlines()
            for index, line in enumerate(lines):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    run["event_errors"].append({"line": index + 1, "reason": str(error)})
                    continue
                if type(event) is not dict or event.get("type") not in {
                    "measurement", "output", "configuration", "cuda", "checkpoint", "load", "case", "complete"
                }:
                    run["event_errors"].append({"line": index + 1, "reason": "not a benchmark event"})
                    continue
                event.update(run_id=run["id"], lane=run["lane"], device=run["device"],
                             image_id=run["image_id"], image_input_id=run["image_input_id"])
                events.append(event)
        run["worker_complete"] = bool(events and events[-1]["type"] == "complete" and not run["event_errors"])
        run["metadata"] = [event for event in events if event["type"] not in {"measurement", "output"}]
        measurements.extend(event for event in events if event["type"] == "measurement")
        outputs.extend(event for event in events if event["type"] == "output")
        for case in manifest["cases"]:
            samples = [event["elapsed_seconds"] for event in events
                       if event["type"] == "measurement" and event["phase"] == "measured" and event["case"] == case]
            if samples:
                case_measurements = [event for event in events if event["type"] == "measurement" and event["case"] == case]
                case_outputs = [event for event in events if event["type"] == "output" and event["case"] == case and event["phase"] == "measured"]
                tokens = next((event["input_tokens"] for event in events if event["type"] == "case" and event["case"] == case), None)
                first = next((event["elapsed_seconds"] for event in case_measurements if event.get("first_generation")), None)
                summaries.append({"run_id": run["id"], "lane": run["lane"], "device": run["device"],
                                  "case": case, "samples": len(samples), "median_seconds": statistics.median(samples),
                                  "p95_seconds": percentile95(samples), "input_tokens": tokens,
                                  "first_generation_seconds": first,
                                  "process_peak_rss_bytes": max(event["process_peak_rss_bytes"] for event in case_measurements),
                                  "gpu_peak_allocated_bytes": max((event["gpu_peak_allocated_bytes"] or 0) for event in case_measurements),
                                  "gpu_peak_reserved_bytes": max((event["gpu_peak_reserved_bytes"] or 0) for event in case_measurements),
                                  "valid_candidates": sum(event["valid_count"] for event in case_outputs),
                                  "total_candidates": sum(len(event["candidates"]) for event in case_outputs),
                                  "candidate_character_lengths": sorted({length for event in case_outputs for length in event["candidate_characters"]})})
    comparisons = []
    for sample in summaries:
        if sample["device"] in {"cpu", "baseline-cpu"}:
            continue
        for reference_device in ("cpu", "baseline-cpu"):
            reference = next((item for item in summaries if item["lane"] == sample["lane"] and
                              item["case"] == sample["case"] and item["device"] == reference_device), None)
            if reference is None:
                continue
            pairs = []
            for target in outputs:
                if target["run_id"] != sample["run_id"] or target["case"] != sample["case"] or target["phase"] != "measured":
                    continue
                original = next((item for item in outputs if item["run_id"] == reference["run_id"] and
                                 item["case"] == target["case"] and item["phase"] == "measured" and
                                 item["repetition"] == target["repetition"]), None)
                if original is not None:
                    pairs.append(compare_outputs(original, target))
            comparisons.append({"lane": sample["lane"], "case": sample["case"], "device": sample["device"],
                                "reference": reference_device,
                                "median_speed_ratio": reference["median_seconds"] / sample["median_seconds"],
                                "output_pairs": pairs})
    expected = {(run["id"], case, repetition) for run in manifest["runs"] for case in manifest["cases"]
                for repetition in range(manifest["repetitions"])}
    measured_keys = [(event["run_id"], event["case"], event["repetition"]) for event in measurements if event["phase"] == "measured"]
    output_keys = [(event["run_id"], event["case"], event["repetition"]) for event in outputs if event["phase"] == "measured"]
    complete = (manifest["status"] == "complete" and bool(manifest["runs"]) and
                all(run.get("exit_code") == 0 and run["worker_complete"] for run in manifest["runs"]) and
                set(measured_keys) == expected and set(output_keys) == expected and
                len(measured_keys) == len(expected) and len(output_keys) == len(expected) and
                len(summaries) == len(manifest["runs"]) * len(manifest["cases"]) and
                all(row["samples"] == manifest["repetitions"] for row in summaries))
    summary = {"status": "complete" if complete else "incomplete", "latencies": summaries, "comparisons": comparisons,
               "startup": [{"run_id": run["id"], "events":
                            [event for event in run["metadata"] if event["type"] in {"load", "checkpoint"}] +
                            [event for event in measurements if event["run_id"] == run["id"] and event.get("first_generation")]}
                           for run in manifest["runs"]]}
    for name, rows in (("measurements", measurements), ("outputs", outputs)):
        (directory / f"{name}.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    text = [f'# NMRPeak CPU/GPU comparison: {summary["status"]}', "",
            f'Source: `{manifest["source_revision"]}`', "",
            "Times include materialization, tokenization, input transfer, generation, and result formatting.",
            "Loading includes model construction, deserialization, and device transfer; filesystem caches are not cleared.",
            "The current CPU stack and matched benchmark CPU stack are separate references.",
            "Host/GPU activity snapshots are in the manifest; this is not a dedicated idle machine.", "",
            "| Model | Case | Device | Samples | Median (s) | p95 (s) |",
            "|---|---|---|---:|---:|---:|"]
    text.extend(f'| {row["lane"]} | {row["case"]} | {row["device"]} | {row["samples"]} | {row["median_seconds"]:.3f} | {row["p95_seconds"]:.3f} |' for row in summaries)
    text.extend(["", "## Speed and output agreement", ""])
    for row in comparisons:
        pairs = row["output_pairs"]
        equal = sum(pair["ranked_equal"] for pair in pairs)
        overlaps = [pair["molecule_overlap"] for pair in pairs if pair["molecule_overlap"] is not None]
        overlap = f"{statistics.mean(overlaps):.3f}" if overlaps else "undefined (no valid molecules)"
        text.append(f'- {row["lane"]}/{row["case"]}, {row["device"]} against {row["reference"]}: '
                    f'{row["median_speed_ratio"]:.2f}×; ranked equality {equal}/{len(pairs)}; mean molecule overlap {overlap}.')
    text.extend(["", "## Startup, inputs, memory, and candidate validity", ""])
    for run in summary["startup"]:
        text.append(f'- {run["run_id"]}: ' + "; ".join(
            f'{key}={value:.3f}s' for event in run["events"] for key, value in event.items() if key.endswith("_seconds")))
    for row in summaries:
        first = row["first_generation_seconds"]
        text.append(f'- {row["run_id"]}/{row["case"]}: {row["input_tokens"]} input tokens; '
                    f'process peak RSS {row["process_peak_rss_bytes"] / 1024**2:.1f} MiB; '
                    f'GPU peak allocated/reserved {row["gpu_peak_allocated_bytes"] / 1024**2:.1f}/{row["gpu_peak_reserved_bytes"] / 1024**2:.1f} MiB; '
                    f'valid candidates {row["valid_candidates"]}/{row["total_candidates"]}; '
                    f'candidate character lengths {row["candidate_character_lengths"]}' +
                    (f'; first actual generation {first:.3f}s.' if first is not None else '.'))
    if not complete:
        text.extend(["", "Incomplete run: inspect manifest status, per-run exit codes, and retained logs before interpreting ratios."])
    (directory / "comparison.md").write_text("\n".join(text) + "\n")
    return summary
