"""Map a host owner through a retired probe on the selected local engine."""

import argparse
from pathlib import Path
import signal
import sys

# The qualified source bundle is the complete import authority under -I -S.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_idmap.probe import ProbeFailure, ProbeRequest, ResourceState, StepError, probe_identity


def mapped_owner(endpoint, image, host_uid, host_gid, *, cancel=None):
    observation = probe_identity(ProbeRequest(
        endpoint=endpoint, image=image, interpreter="/usr/local/bin/python3",
        user_uid=0, user_gid=0,
    ), cancel=cancel)
    return (int(observation.uid_map.translate_inverse(host_uid)),
            int(observation.gid_map.translate_inverse(host_gid)))


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
    previous = {number: signal.signal(number, cancellation.request)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    retired = False
    status = 0
    try:
        retired = True
        request = ProbeRequest(
            endpoint=args.endpoint, image=args.image,
            interpreter="/usr/local/bin/python3", user_uid=0, user_gid=0,
        )
        retired = False
        observation = probe_identity(request, cancel=cancellation)
        retired = True
        uid = observation.uid_map.translate_inverse(args.host_uid)
        gid = observation.gid_map.translate_inverse(args.host_gid)
        if not cancellation.is_set():
            print(f"{uid} {gid}")
    except ProbeFailure as failure:
        problems = (failure.problem, *failure.retirement.problems)
        # Container retirement does not erase an unresolved native Docker client.
        retired = (failure.retirement.state in (ResourceState.NOT_CREATED, ResourceState.RETIRED)
                   and all(problem.category != "client_cleanup" for problem in problems))
        print(f"Cannot map acquisition owner: {failure.problem.message}", file=sys.stderr)
        for problem in failure.retirement.problems:
            print(f"Probe cleanup: {problem.message}", file=sys.stderr)
        if failure.handle is not None:
            container_id = failure.handle.container_id
            if not container_id:
                container_id = "not confirmed"
            print(f"Probe operation {failure.handle.operation_token}; "
                  f"container {container_id}; "
                  f"retirement {failure.retirement.state}.", file=sys.stderr)
        status = 2
    except (ValueError, StepError) as failure:
        print(f"Cannot map acquisition owner: {failure}", file=sys.stderr)
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
