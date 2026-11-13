"""Project identities through one retired probe on the selected local engine."""

import argparse
import signal
import sys
from dataclasses import replace
from pathlib import Path

from linux_idmap import Identifier
from linux_idmap.probe import ProbeFailure, ProbeRequest, ProbeResources, ResourceState, StepError, probe_identity


class Cancellation:
    def __init__(self):
        self.signal = 0

    def request(self, number, _frame):
        if not self.signal:
            self.signal = number

    def is_set(self):
        return bool(self.signal)


def _inputs(args):
    policy = (args.interpreter, args.user_uid, args.user_gid,
              args.memory_bytes, args.pids_limit, args.nano_cpus)
    if args.maps is None:
        if (args.host_uid is None or args.host_gid is None or args.userns is not None
                or any(value is not None for value in policy)):
            raise ValueError("Supply both host-owner IDs, or use --map with explicit probe policy.")
        request = ProbeRequest(args.endpoint, args.image, "/usr/local/bin/python3", 0, 0)
        return request, (("uid", "inverse", Identifier(args.host_uid)),
                         ("gid", "inverse", Identifier(args.host_gid)))
    if args.host_uid is not None or args.host_gid is not None:
        raise ValueError("Do not combine host-owner IDs with --map projections.")
    if any(value is None for value in policy):
        raise ValueError("Mapping projections require an explicit interpreter, probe UID/GID and all three resource ceilings.")
    if not (len(args.maps) <= 512):
        raise ValueError("Request at most 512 identity projections.")
    projections = []
    for (axis, direction, identifier) in args.maps:
        if axis not in ("uid", "gid") or direction not in ("forward", "inverse"):
            raise ValueError("Each --map selects uid or gid, forward or inverse, and one Linux ID.")
        projections.append((axis, direction, Identifier.of(identifier)))
    request = ProbeRequest(
        args.endpoint, args.image, args.interpreter, args.user_uid, args.user_gid,
        resources=ProbeResources(args.memory_bytes, args.pids_limit, args.nano_cpus),
    )
    if args.userns is not None:
        request = replace(request, userns_mode=args.userns)
    return request, projections


def _project(observation, projections):
    values = []
    for (axis, direction, identifier) in projections:
        if axis == "uid":
            mapping = observation.uid_map
        else:
            mapping = observation.gid_map
        if direction == "forward":
            values.append(mapping.translate(identifier))
        else:
            values.append(mapping.translate_inverse(identifier))
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--host-uid", type=int)
    parser.add_argument("--host-gid", type=int)
    parser.add_argument("--map", dest="maps", action="append", nargs=3, metavar=("AXIS", "DIRECTION", "ID"))
    parser.add_argument("--interpreter")
    parser.add_argument("--user-uid", type=int)
    parser.add_argument("--user-gid", type=int)
    parser.add_argument("--userns", choices=("default", "host"))
    parser.add_argument("--memory-bytes", type=int)
    parser.add_argument("--pids-limit", type=int)
    parser.add_argument("--nano-cpus", type=int)
    parser.add_argument("--retired-receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    cancellation = Cancellation()
    previous = {number: signal.signal(number, cancellation.request) for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    retired = False
    status = 0
    try:
        retired = True
        request, projections = _inputs(args)
        retired = False
        observation = probe_identity(request, cancel=cancellation)
        retired = True
        values = _project(observation, projections)
    except ProbeFailure as failure:
        problems = (failure.problem, *failure.retirement.problems)
        # Container retirement does not erase an unresolved native Docker client.
        retired = failure.retirement.state in (ResourceState.NOT_CREATED, ResourceState.RETIRED) and all(
            problem.category != "client_cleanup" for problem in problems
        )
        sys.stderr.write(f"Cannot map engine identity: {failure.problem.message}\n")
        for problem in failure.retirement.problems:
            sys.stderr.write(f"Probe cleanup: {problem.message}\n")
        if failure.handle is not None:
            container_id = failure.handle.container_id
            if not container_id:
                container_id = "not confirmed"
            sys.stderr.write(
                f"Probe operation {failure.handle.operation_token}; "
                f"container {container_id}; "
                f"retirement {failure.retirement.state}.\n"
            )
        status = 2
    except (ValueError, StepError) as failure:
        sys.stderr.write(f"Cannot map engine identity: {failure}\n")
        status = 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if retired:
            with args.retired_receipt.open("x", encoding="ascii") as receipt:
                receipt.write("retired\n")
    # Receipt and restored caller handlers precede any usable projection output.
    if status == 0 and not cancellation.is_set():
        sys.stdout.write(" ".join(map(str, values)) + "\n")
    return 128 + cancellation.signal if cancellation.is_set() else status


if __name__ == "__main__":
    raise SystemExit(main())
