"""Run a probe and print its maps or requested UID/GID translations."""

import argparse
from dataclasses import replace
import json
import signal
import sys

from .core import Identifier
from .probe import Problem, ProbeFailure, ProbeRequest, ResourceState, Retirement, StepError, probe_identity


class _SignalCancellation:
    def __init__(self):
        self.requested = False

    def request(self, _number, _frame):
        # A signal can interrupt a held lock, including threading.Event's lock.
        self.requested = True

    def is_set(self) -> bool:
        return self.requested


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise StepError("invalid-policy", "invalid or missing arguments; use --help")


def _parser():
    parser = _Parser(prog="linux-idmap-probe", allow_abbrev=False)
    parser.add_argument("--endpoint", required=True, help="already-selected unix:/// Docker endpoint")
    parser.add_argument("--image", required=True, help="approved image already present on that engine")
    parser.add_argument("--interpreter", required=True, help="absolute Python path inside that image")
    parser.add_argument("--user-uid", required=True, type=Identifier.of)
    parser.add_argument("--user-gid", required=True, type=Identifier.of)
    parser.add_argument("--userns", choices=("default", "host"))
    parser.add_argument("--uid", action="append", type=Identifier.of)
    parser.add_argument("--gid", action="append", type=Identifier.of)
    parser.add_argument("--values", action="store_true", help="print only projected decimal IDs for shell callers")
    parser.add_argument("--timeout", type=float, help="override the measurement time bound in seconds")
    return parser


def _emit(payload: dict, *, values_mode: bool):
    if values_mode and payload["ok"]:
        values = payload["mapped_uids"] + payload["mapped_gids"]
        print(" ".join(map(str, values)))
    else:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")),
              file=sys.stderr if values_mode else sys.stdout)


def main(argv: list[str] | None = None) -> int:
    handlers = {}
    arguments = list(sys.argv[1:] if argv is None else argv)
    # --values routes parse errors to stderr before parsing can complete; no numbers are emitted on failure; tests/test_cli.py::CliTests.test_values_parse_failure_keeps_stdout_empty.
    values_mode = "--values" in arguments
    try:
        options = _parser().parse_args(arguments)
        if options.uid is None:
            options.uid = []
        if options.gid is None:
            options.gid = []
        # After valid parsing the boolean switch, rather than raw tokens, owns output routing; tests/test_cli.py::CliTests.test_values_are_only_requested_decimal_ids_in_order.
        values_mode = options.values
        if len(options.uid) + len(options.gid) > 512:
            raise StepError("invalid-policy", "request at most 512 projected identities")
        request = ProbeRequest(options.endpoint, options.image, options.interpreter,
                               options.user_uid, options.user_gid)
        if options.userns is not None:
            request = replace(request, userns_mode=options.userns)
        if options.timeout is not None:
            request = replace(request, bounds=replace(request.bounds, operation_seconds=options.timeout))
        cancellation = _SignalCancellation()
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[number] = signal.signal(number, cancellation.request)
        result = probe_identity(request, cancel=cancellation)
        try:
            projected_uids = [result.uid_map.translate(uid) for uid in options.uid]
            projected_gids = [result.gid_map.translate(gid) for gid in options.gid]
        except ValueError as error:
            raise ProbeFailure(Problem("unmapped-id", str(error)),
                Retirement(ResourceState.RETIRED, result.container_id)) from error
        payload = result.as_dict()
        payload.update(mapped_uids=projected_uids, mapped_gids=projected_gids)
        if cancellation.is_set():
            raise ProbeFailure(Problem("cancelled", "operation was cancelled before output"),
                               Retirement(ResourceState.RETIRED, result.container_id))
        _emit(payload, values_mode=values_mode)
        return 0
    # silent-defaults: allow-exception-outcome -- Structured operation failure always produces a positive exit status and never releases maps; tests/test_cli.py::CliTests.
    except ProbeFailure as failure:
        _emit(failure.as_dict(), values_mode=values_mode)
        if failure.retirement.state == ResourceState.UNRESOLVED:
            return 3
        # silent-defaults: allow-contract -- Cancellation has shell status 130; all other settled operation failures remain nonzero status 1; tests/test_cli.py::CliTests.
        return 130 if failure.problem.category == "cancelled" else 1
    # silent-defaults: allow-exception-outcome -- Invalid caller policy is a structured refusal with status 2 and known-not-created state; tests/test_cli.py::CliTests.test_missing_arguments_fail_before_creation.
    except StepError as error:
        failure = ProbeFailure(Problem(error.category, str(error)), Retirement(ResourceState.NOT_CREATED))
        _emit(failure.as_dict(), values_mode=values_mode)
        return 2
    finally:
        for number, previous in handlers.items():
            signal.signal(number, previous)


if __name__ == "__main__":
    raise SystemExit(main())
