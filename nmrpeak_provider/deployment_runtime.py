"""Compose deployment grants inside the actual installed provider image.

The host owns Docker and its source-file grants. SDK admission owns binding and
route semantics; this adapter adds NMR's historical journal namespace identity.
It reads no credential, CA or resolver contents and opens no network connection.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from provider_core.deployment_binding import input_paths, load_deployment_bindings
from provider_core.deployment_route import NamedRouteVolume, render_api_route
from provider_core.diagnostics import failure_evidence

from .provider_diagnostics import describe_nmrpeak_failure

from .journal_identity import journal_api_authority_id


def _binding_facts(binding):
    return {
        "provider_ref": binding.api["provider_ref"],
        "api_origin": binding.api["origin"],
        "api_topology": binding.api["topology"],
        "journal_authority_id": journal_api_authority_id(
            origin=binding.api["origin"], topology=binding.api["topology"],
        ),
    }


def project_provider_binding(definition, binding_name, *, image, namespace, input_sources):
    """Return the exact SDK route and grants for one explicitly selected binding."""
    binding = load_deployment_bindings(definition).selected(binding_name)
    route = render_api_route(
        binding, image=image, interpreter="/usr/local/bin/python3.12", namespace=namespace,
        uid=65532, gid=65532, storage=NamedRouteVolume(), input_sources=input_sources,
        service_name="api",
    )
    return {
        "api_route": asdict(route),
        "delivery": {"ipc_volume": binding.ipc_volume, "socket_path": str(binding.socket_path),
                     "socket_group_id": binding.socket_group_id},
        "input_paths": input_paths((binding,)),
        "credential_file": binding.api["credential_file"],
        **_binding_facts(binding),
    }


def validate_captured_runtime():
    """Admit the captured binding and credential before host runtime effects."""
    from .input_interpreter import INTERPRETER_CONFIG_DIRECTORY
    from .openai_chat_interpreter import load_openai_chat_endpoint_specs
    from .provider_startup import load_provider_startup

    startup = load_provider_startup()
    load_openai_chat_endpoint_specs(INTERPRETER_CONFIG_DIRECTORY)
    return {
        **_binding_facts(startup.binding),
        "frozen_generation_id": startup.configured.frozen_generation_id,
        "proxy_socket": startup.configured.api_proxy_socket,
    }


def validate_captured_binding(binding_name, proxy_socket):
    from .provider_startup import load_captured_api_binding

    _definition, binding, _settings = load_captured_api_binding(binding_name, proxy_socket)
    return _binding_facts(binding) | {"proxy_socket": proxy_socket}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    project = commands.add_parser("project", help="Project one selected SDK binding and its grants")
    project.add_argument("--definition", type=Path, required=True)
    project.add_argument("--binding", required=True)
    project.add_argument("--image", required=True)
    project.add_argument("--namespace", required=True)
    project.add_argument("--input-source", nargs=2, action="append", default=[])
    commands.add_parser("validate", help="Validate the captured runtime inputs without opening a connection")
    binding = commands.add_parser("validate-binding", help="Validate captured API inputs without science or interpreter grants")
    binding.add_argument("--binding", required=True)
    binding.add_argument("--proxy-socket", required=True)
    options = parser.parse_args(arguments)
    try:
        if options.operation == "validate":
            projection = validate_captured_runtime()
        elif options.operation == "validate-binding":
            projection = validate_captured_binding(options.binding, options.proxy_socket)
        else:
            sources = dict(options.input_source)
            if len(sources) != len(options.input_source):
                raise ValueError("Cannot project provider inputs: each original path must be mapped once")
            projection = project_provider_binding(
                options.definition, options.binding, image=options.image, namespace=options.namespace,
                input_sources=sources,
            )
        print(json.dumps(projection, ensure_ascii=True, sort_keys=True))
        return 0
    except BaseException as cause:
        print(json.dumps({"operation": "project NMRPeak deployment binding", "failure": failure_evidence(
            cause, secrets=(), describe_owner=describe_nmrpeak_failure,
        )}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
