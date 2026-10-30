"""Project SDK shared grants and validate captured provider API inputs."""

import argparse
import json
from pathlib import Path
import sys

from provider_core.diagnostics import failure_evidence
from provider_core.api_config import require_api_fields
from provider_core.shared_grants import shared_grants

from .journal_identity import journal_api_authority_id
from .provider_config import decode_provider_template
from .provider_diagnostics import describe_nmrpeak_failure


def api_facts(api):
    return {
        "provider_ref": api["provider_ref"],
        "api_origin": api["origin"],
        "api_topology": api["topology"],
        "journal_authority_id": journal_api_authority_id(origin=api["origin"], topology=api["topology"]),
    }


def project_provider_inputs(provider_file, settings_file):
    policy = decode_provider_template(Path(provider_file).read_bytes())
    grants = shared_grants(settings_file).compose()
    return project_api_inputs(policy) | {"shared": grants}


def project_api_inputs(policy):
    require_api_fields(policy.api)
    inputs = [policy.api["credential_file"]]
    if "ca_file" in policy.api:
        inputs.append(policy.api["ca_file"])
    return {"input_paths": inputs, "credential_file": policy.api["credential_file"], **api_facts(policy.api)}


def validate_captured_runtime():
    from .input_interpreter import INTERPRETER_CONFIG_DIRECTORY
    from .openai_chat_interpreter import load_openai_chat_endpoint_specs
    from .provider_startup import load_provider_startup

    startup = load_provider_startup()
    load_openai_chat_endpoint_specs(INTERPRETER_CONFIG_DIRECTORY)
    return {**api_facts(startup.configured.policy.api),
            "frozen_generation_id": startup.configured.frozen_generation_id,
            "proxy_socket": startup.configured.api_proxy_socket,
            "outbox_socket": startup.configured.outbox_socket}


def validate_captured_api(proxy_socket):
    from .provider_startup import load_maintenance_api

    settings, provider_ref, _publication = load_maintenance_api(proxy_socket)
    api = {"provider_ref": provider_ref, "origin": settings.identity.origin, "topology": settings.expected_topology}
    return api_facts(api) | {"proxy_socket": proxy_socket}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    project = commands.add_parser("project")
    project.add_argument("--provider", type=Path, required=True)
    project.add_argument("--shared-settings", type=Path, required=True)
    api_project = commands.add_parser("project-api")
    api_project.add_argument("--provider", type=Path, required=True)
    commands.add_parser("validate")
    api = commands.add_parser("validate-api")
    api.add_argument("--proxy-socket", required=True)
    options = parser.parse_args(arguments)
    try:
        if options.operation == "validate":
            projection = validate_captured_runtime()
        elif options.operation == "validate-api":
            projection = validate_captured_api(options.proxy_socket)
        elif options.operation == "project-api":
            projection = project_api_inputs(decode_provider_template(options.provider.read_bytes()))
        else:
            projection = project_provider_inputs(options.provider, options.shared_settings)
        print(json.dumps(projection, ensure_ascii=True, sort_keys=True))
        return 0
    except BaseException as cause:
        print(json.dumps({"operation": "project NMRPeak shared grants", "failure": failure_evidence(
            cause, secrets=(), describe_owner=describe_nmrpeak_failure,
        )}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
