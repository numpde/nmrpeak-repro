"""Admit historical journal hold fields without live API or recovery authority.

Frozen from the NMR v1/v2 record profiles at upstream OpenAPI SHA256
f538c902dd0466355a602d583c267eac3b626474d2cbe0ba4abc3c43f6a7153c.
Only code membership, action and text limits were consulted by these readers.
"""

import re

EVIDENCE = {'detail': {'maxLength': 1024,
            'minLength': 1,
            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
            'type': 'string',
            'x-nmr-max-utf8-bytes': 1024},
 'request_id': {'maxLength': 128,
                'minLength': 1,
                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                'type': 'string'}}

CONFLICT_RECOVERY = {'execution_attempt_complete': {'deployment_upload_record_limit_reached': {'action': 'reconcile_state'},
                                'deployment_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state'},
                                'execution_attempt_completion_after_failure': {'action': 'do_not_resend'},
                                'execution_attempt_completion_replay_mismatch': {'action': 'reconcile_original'},
                                'execution_attempt_failure_after_success': {'action': 'do_not_resend'},
                                'execution_attempt_failure_replay_mismatch': {'action': 'reconcile_original'},
                                'execution_attempt_outcome_expired': {'action': 'do_not_resend'},
                                'execution_attempt_progress_regression': {'action': 'reconcile_state'},
                                'execution_attempt_progress_terminal': {'action': 'reconcile_state'},
                                'job_attempt_limit_reached': {'action': 'reconcile_state'},
                                'job_open_pending_uploads': {'action': 'reconcile_state'},
                                'job_provider_attempt_limit_reached': {'action': 'reconcile_state'},
                                'job_state_change_cancelled': {'action': 'reconcile_state'},
                                'job_upload_selection_cancelled': {'action': 'reconcile_state'},
                                'job_upload_selection_pending': {'action': 'reconcile_state'},
                                'job_upload_selection_removal_scheduled': {'action': 'reconcile_state'},
                                'job_upload_selection_retention_expired': {'action': 'reconcile_state'},
                                'operation_conflict': {'action': 'reconcile_state'},
                                'operation_reference_conflict': {'action': 'reconcile_state'},
                                'project_job_limit_reached': {'action': 'reconcile_state'},
                                'project_operation_record_limit_reached': {'action': 'reconcile_state'},
                                'project_purge_in_progress': {'action': 'reconcile_state'},
                                'project_upload_record_limit_reached': {'action': 'reconcile_state'},
                                'project_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state'},
                                'provider_attempt_key_conflict': {'action': 'reconcile_state'},
                                'upload_byte_length_limit_exceeded': {'action': 'reconcile_state'},
                                'upload_finalize_bytes_absent': {'action': 'reconcile_state'},
                                'upload_finalize_bytes_incomplete': {'action': 'reconcile_state'},
                                'upload_finalize_removal_scheduled': {'action': 'reconcile_state'},
                                'upload_linked_to_job': {'action': 'reconcile_state'},
                                'upload_publish_already_finalized': {'action': 'reconcile_state'},
                                'upload_publish_removal_scheduled': {'action': 'reconcile_state'},
                                'upload_read_not_finalized': {'action': 'reconcile_state'},
                                'upload_read_removal_scheduled': {'action': 'reconcile_state'}},
 'execution_attempt_fail': {'deployment_upload_record_limit_reached': {'action': 'reconcile_state'},
                            'deployment_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state'},
                            'execution_attempt_completion_after_failure': {'action': 'do_not_resend'},
                            'execution_attempt_completion_replay_mismatch': {'action': 'reconcile_original'},
                            'execution_attempt_failure_after_success': {'action': 'do_not_resend'},
                            'execution_attempt_failure_replay_mismatch': {'action': 'reconcile_original'},
                            'execution_attempt_outcome_expired': {'action': 'do_not_resend'},
                            'execution_attempt_progress_regression': {'action': 'reconcile_state'},
                            'execution_attempt_progress_terminal': {'action': 'reconcile_state'},
                            'job_attempt_limit_reached': {'action': 'reconcile_state'},
                            'job_open_pending_uploads': {'action': 'reconcile_state'},
                            'job_provider_attempt_limit_reached': {'action': 'reconcile_state'},
                            'job_state_change_cancelled': {'action': 'reconcile_state'},
                            'job_upload_selection_cancelled': {'action': 'reconcile_state'},
                            'job_upload_selection_pending': {'action': 'reconcile_state'},
                            'job_upload_selection_removal_scheduled': {'action': 'reconcile_state'},
                            'job_upload_selection_retention_expired': {'action': 'reconcile_state'},
                            'operation_conflict': {'action': 'reconcile_state'},
                            'operation_reference_conflict': {'action': 'reconcile_state'},
                            'project_job_limit_reached': {'action': 'reconcile_state'},
                            'project_operation_record_limit_reached': {'action': 'reconcile_state'},
                            'project_purge_in_progress': {'action': 'reconcile_state'},
                            'project_upload_record_limit_reached': {'action': 'reconcile_state'},
                            'project_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state'},
                            'provider_attempt_key_conflict': {'action': 'reconcile_state'},
                            'upload_byte_length_limit_exceeded': {'action': 'reconcile_state'},
                            'upload_finalize_bytes_absent': {'action': 'reconcile_state'},
                            'upload_finalize_bytes_incomplete': {'action': 'reconcile_state'},
                            'upload_finalize_removal_scheduled': {'action': 'reconcile_state'},
                            'upload_linked_to_job': {'action': 'reconcile_state'},
                            'upload_publish_already_finalized': {'action': 'reconcile_state'},
                            'upload_publish_removal_scheduled': {'action': 'reconcile_state'},
                            'upload_read_not_finalized': {'action': 'reconcile_state'},
                            'upload_read_removal_scheduled': {'action': 'reconcile_state'}}}


def admitted_evidence_text(value: object, profile: dict) -> str | None:
    if type(value) is not str or not profile["minLength"] <= len(value) <= profile["maxLength"]:
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    if len(encoded) > profile.get("x-nmr-max-utf8-bytes", 4 * profile["maxLength"]):
        return None
    if "pattern" in profile and re.search(profile["pattern"], value) is None:
        return None
    if "not" in profile and re.search(profile["not"]["pattern"], value) is not None:
        return None
    return value
