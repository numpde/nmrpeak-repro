"""Map a host owner through a retired probe on the selected local engine."""

import argparse
import signal
import sys
from pathlib import Path

from linux_idmap.probe import ProbeFailure, ProbeRequest, ResourceState, StepError, probe_identity


class Cancellation:
    def __init__(self):
        self.signal = 0

    def request(self, number, _frame):
        if not self.signal:
            self.signal = number

    def is_set(self):
        return bool(self.signal)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--host-uid", required=True, type=int)
    parser.add_argument("--host-gid", required=True, type=int)
    parser.add_argument("--retired-receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    cancellation = Cancellation()
    previous = {number: signal.signal(number, cancellation.request) for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    retired = False
    status = 0
    try:
        retired = True
        request = ProbeRequest(
            endpoint=args.endpoint,
            image=args.image,
            interpreter="/usr/local/bin/python3",
            user_uid=0,
            user_gid=0,
        )
        retired = False
        observation = probe_identity(request, cancel=cancellation)
        retired = True
        uid = observation.uid_map.translate_inverse(args.host_uid)
        gid = observation.gid_map.translate_inverse(args.host_gid)
        if not cancellation.is_set():
            sys.stdout.write(f"{uid} {gid}\n")
    except ProbeFailure as failure:
        problems = (failure.problem, *failure.retirement.problems)
        # Container retirement does not erase an unresolved native Docker client.
        retired = failure.retirement.state in (ResourceState.NOT_CREATED, ResourceState.RETIRED) and all(
            problem.category != "client_cleanup" for problem in problems
        )
        sys.stderr.write(f"Cannot map acquisition owner: {failure.problem.message}\n")
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
        sys.stderr.write(f"Cannot map acquisition owner: {failure}\n")
        status = 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if retired:
            with args.retired_receipt.open("x", encoding="ascii") as receipt:
                receipt.write("retired\n")
    return 128 + cancellation.signal if cancellation.is_set() else status


if __name__ == "__main__":
    raise SystemExit(main())
