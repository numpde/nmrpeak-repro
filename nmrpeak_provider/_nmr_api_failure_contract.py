"""Generated provider Problem wire facts; do not edit."""
OPENAPI_SHA256 = 'f538c902dd0466355a602d583c267eac3b626474d2cbe0ba4abc3c43f6a7153c'
TERMINAL_REPORT_CONDITIONS = {'conditions': {'held': {'code': 'terminal_report_delivery_held',
                         'message': 'At its latest update, the provider reported that it had '
                                    'retained an outcome report and paused publication for '
                                    'operator review. Delivery is not confirmed.',
                         'next_action': 'Contact the provider operator with the Attempt '
                                        'reference.'},
                'reconciling': {'code': 'terminal_report_delivery_reconciling',
                                'message': 'At its latest update, the provider reported that it '
                                           'had retained an outcome report and paused publication '
                                           'while automatically checking the API outcome. Delivery '
                                           'is not confirmed.',
                                'next_action': "Wait for the provider's checks. If this remains "
                                               'unresolved, contact the provider operator with the '
                                               'Attempt reference.'},
                'retrying': {'code': 'terminal_report_delivery_retryable',
                             'message': 'At its latest update, the provider reported that it had '
                                        'retained an outcome report and that automatic retries '
                                        'were pending. Delivery is not confirmed.',
                             'next_action': 'Wait for an update. If this remains unresolved, '
                                            'contact the provider operator with the Attempt '
                                            'reference.'}},
 'version': 1}
EVIDENCE = {'detail': {'maxLength': 1024,
            'minLength': 1,
            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
            'type': 'string',
            'x-nmr-max-utf8-bytes': 1024},
 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
 'request_id': {'maxLength': 128,
                'minLength': 1,
                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                'type': 'string'}}
OPERATIONS = {'execution_attempt_complete': {400: (8,),
                                401: (1,),
                                403: (2,),
                                404: (9,),
                                408: (3,),
                                409: (10,),
                                413: (11,),
                                414: (4,),
                                431: (5,),
                                500: (6,),
                                503: (12, 7)},
 'execution_attempt_fail': {400: (8,),
                            401: (1,),
                            403: (2,),
                            404: (9,),
                            408: (3,),
                            409: (10,),
                            413: (11,),
                            414: (4,),
                            431: (5,),
                            500: (6,),
                            503: (12, 7)},
 'execution_attempt_progress': {400: (8,),
                                401: (1,),
                                403: (2,),
                                404: (9,),
                                408: (3,),
                                409: (10,),
                                413: (11,),
                                414: (4,),
                                431: (5,),
                                500: (6,),
                                503: (12, 7)},
 'execution_attempt_read': {400: (13,),
                            401: (1,),
                            403: (2,),
                            404: (9,),
                            408: (3,),
                            414: (4,),
                            431: (5,),
                            500: (6,),
                            503: (7,)},
 'execution_attempt_start': {400: (8,),
                             401: (1,),
                             403: (2,),
                             404: (9,),
                             408: (3,),
                             409: (10,),
                             413: (11,),
                             414: (4,),
                             431: (5,),
                             500: (6,),
                             503: (12, 7)},
 'execution_attempts_list': {400: (0,),
                             401: (1,),
                             403: (2,),
                             408: (3,),
                             414: (4,),
                             431: (5,),
                             500: (6,),
                             503: (7,)},
 'job_input_read': {400: (0,),
                    401: (1,),
                    403: (2,),
                    404: (9,),
                    408: (3,),
                    414: (4,),
                    431: (5,),
                    500: (6,),
                    503: (7,)},
 'job_upload_read_capability': {400: (13,),
                                401: (1,),
                                403: (2,),
                                404: (9,),
                                408: (3,),
                                409: (10,),
                                414: (4,),
                                431: (5,),
                                500: (6,),
                                503: (12, 7)},
 'job_upload_set_read': {400: (13,),
                         401: (1,),
                         403: (2,),
                         404: (9,),
                         408: (3,),
                         414: (4,),
                         431: (5,),
                         500: (6,),
                         503: (7,)},
 'jobs_list': {400: (0,),
               401: (1,),
               403: (2,),
               408: (3,),
               414: (4,),
               431: (5,),
               500: (6,),
               503: (7,)},
 'provider_hello': {400: (8,),
                    401: (1,),
                    403: (2,),
                    404: (9,),
                    408: (3,),
                    413: (11,),
                    414: (4,),
                    431: (5,),
                    500: (6,),
                    503: (12, 7)}}
RECOVERY = {'execution_attempt_complete': {'description': "Retry saving the computation's result by resending "
                                               'the unchanged complete request body, including '
                                               'execution_attempt_ref, to the same endpoint. An '
                                               'identical retry does not create a second result. '
                                               'Do not rerun the computation or report a different '
                                               'terminal outcome to recover this request.',
                                'mode': 'exact_completion',
                                'version': 1},
 'execution_attempt_fail': {'description': "Retry saving the computation's failure report by "
                                           'resending the unchanged fail request body, including '
                                           'execution_attempt_ref, to the same endpoint. An '
                                           'identical retry does not change an outcome already '
                                           'recorded. Do not rerun the computation or report a '
                                           'different terminal outcome to recover this request.',
                            'mode': 'exact_failure',
                            'version': 1},
 'execution_attempt_progress': {'description': 'Retry the same phase and condition_code for the '
                                               'same execution_attempt_ref only if they still '
                                               "represent the provider's latest observation. "
                                               'Otherwise, report the latest observation instead; '
                                               'do not automatically resend an older report. A '
                                               'retry can overwrite a newer condition recorded by '
                                               'another request. Equal current facts return their '
                                               'stored timestamp, not a historical receipt. Moving '
                                               'from running back to preparing is rejected; '
                                               'terminal Attempts reject changed progress. This '
                                               'response concerns recording progress, not '
                                               'starting, stopping or restarting work, and does '
                                               'not confirm a final outcome.',
                                'mode': 'latest_progress',
                                'version': 1},
 'execution_attempt_read': {'description': 'This route performs no domain mutation; retry '
                                           'according to provider policy. Every retry uses a fresh '
                                           'signature and fresh nonce.',
                            'mode': 'read',
                            'version': 1},
 'execution_attempt_start': {'description': 'Resend the unchanged start request body, including '
                                            'job_ref and provider_attempt_key, to the same '
                                            'endpoint. A successful retry creates the Attempt or '
                                            'returns the existing Attempt with its current state. '
                                            'Do not use a new provider_attempt_key to recover this '
                                            'request. An in_progress Attempt does not prove '
                                            "computation has started: check your worker's records "
                                            'before starting work, and never repeat work already '
                                            'running or finished. Do not start work on a '
                                            'succeeded, failed, or expired Attempt.',
                             'mode': 'same_start',
                             'version': 1},
 'execution_attempts_list': {'description': 'This route performs no domain mutation; retry '
                                            'according to provider policy. Every retry uses a '
                                            'fresh signature and fresh nonce.',
                             'mode': 'read',
                             'version': 1},
 'job_input_read': {'description': 'This route performs no domain mutation; retry according to '
                                   'provider policy. Every retry uses a fresh signature and fresh '
                                   'nonce.',
                    'mode': 'read',
                    'version': 1},
 'job_upload_read_capability': {'description': 'Follow the specific error code and detail before '
                                               'retrying. If the provider still needs to download '
                                               "this Job's Upload, repeat the bodyless POST "
                                               'read-capability request for the same job_ref and '
                                               'upload_ref. A successful retry rechecks current '
                                               'provider access and issues a new download '
                                               'capability; it does not recover a previously '
                                               'issued capability. Issuance may already have '
                                               "extended the Upload's access lifetime even though "
                                               'no capability was returned. New issuance may '
                                               'extend it further and delay safe removal. This '
                                               'response concerns issuing download access, not '
                                               'whether file bytes were downloaded.',
                                'mode': 'new_read_capability',
                                'version': 1},
 'job_upload_set_read': {'description': 'This route performs no domain mutation; retry according '
                                        'to provider policy. Every retry uses a fresh signature '
                                        'and fresh nonce.',
                         'mode': 'read',
                         'version': 1},
 'jobs_list': {'description': 'This route performs no domain mutation; retry according to provider '
                              'policy. Every retry uses a fresh signature and fresh nonce.',
               'mode': 'read',
               'version': 1},
 'provider_hello': {'description': "If publication is still intended, send the provider's latest "
                                   'complete hello announcement, including its display name, '
                                   'description and analysis offerings. Do not automatically '
                                   'resend an older announcement: a retry replaces the whole '
                                   'published snapshot and can overwrite newer changes. Offerings '
                                   'omitted from the new announcement are removed from that '
                                   'snapshot. Even an identical repeat is a new hello and '
                                   'heartbeat with a new server acceptance time, not recovery of '
                                   'the original acknowledgement. Acceptance records the '
                                   'announcement; it does not confirm that analysis work started '
                                   'or completed.',
                    'mode': 'latest_hello',
                    'version': 1}}
SEND_EFFECTS = {'execution_attempt_complete': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                          'request_query_not_supported': 'no_assertion'}},
                                401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                    'authentication_nonce_reused': 'no_domain_change',
                                                                                    'authentication_window_closed': 'no_domain_change',
                                                                                    'signature_created_in_future': 'no_domain_change',
                                                                                    'signature_expired': 'no_domain_change'}},
                                403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                                404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                                408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                                409: {'urn:nmr-api:problem:operation-conflict': {'deployment_upload_record_limit_reached': 'no_assertion',
                                                                                 'deployment_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                                 'execution_attempt_completion_after_failure': 'no_domain_change',
                                                                                 'execution_attempt_completion_replay_mismatch': 'no_domain_change',
                                                                                 'execution_attempt_failure_after_success': 'no_domain_change',
                                                                                 'execution_attempt_failure_replay_mismatch': 'no_domain_change',
                                                                                 'execution_attempt_outcome_expired': 'no_domain_change',
                                                                                 'execution_attempt_progress_regression': 'no_assertion',
                                                                                 'execution_attempt_progress_terminal': 'no_assertion',
                                                                                 'job_attempt_limit_reached': 'no_domain_change',
                                                                                 'job_open_pending_uploads': 'no_assertion',
                                                                                 'job_provider_attempt_limit_reached': 'no_domain_change',
                                                                                 'job_state_change_cancelled': 'no_assertion',
                                                                                 'job_upload_selection_cancelled': 'no_assertion',
                                                                                 'job_upload_selection_pending': 'no_assertion',
                                                                                 'job_upload_selection_removal_scheduled': 'no_assertion',
                                                                                 'job_upload_selection_retention_expired': 'no_assertion',
                                                                                 'operation_conflict': 'no_assertion',
                                                                                 'operation_reference_conflict': 'no_assertion',
                                                                                 'project_job_limit_reached': 'no_assertion',
                                                                                 'project_operation_record_limit_reached': 'no_assertion',
                                                                                 'project_purge_in_progress': 'no_assertion',
                                                                                 'project_upload_record_limit_reached': 'no_assertion',
                                                                                 'project_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                                 'provider_attempt_key_conflict': 'no_domain_change',
                                                                                 'upload_byte_length_limit_exceeded': 'no_assertion',
                                                                                 'upload_finalize_bytes_absent': 'no_assertion',
                                                                                 'upload_finalize_bytes_incomplete': 'no_assertion',
                                                                                 'upload_finalize_removal_scheduled': 'no_assertion',
                                                                                 'upload_linked_to_job': 'no_assertion',
                                                                                 'upload_publish_already_finalized': 'no_assertion',
                                                                                 'upload_publish_removal_scheduled': 'no_assertion',
                                                                                 'upload_read_not_finalized': 'no_assertion',
                                                                                 'upload_read_removal_scheduled': 'no_assertion'}},
                                413: {'urn:nmr-api:problem:request-content-too-large': {'request_content_too_large': 'no_assertion'}},
                                414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                           'request_query_too_large': 'no_assertion'}},
                                431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                              'request_header_count_too_large': 'no_assertion'}},
                                500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                             'internal_error': 'no_assertion',
                                                                             'job_open_inputs_inconsistent': 'no_assertion',
                                                                             'request_signature_processing_failed': 'no_domain_change',
                                                                             'response_size_limit_exceeded': 'no_assertion',
                                                                             'response_validation_failed': 'no_assertion',
                                                                             'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                             'upload_finalize_length_mismatch': 'no_assertion',
                                                                             'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                             'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                             'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                                503: {'urn:nmr-api:problem:mutation-outcome-unconfirmed': {'http_exchange_deadline_exceeded': 'unconfirmed',
                                                                                           'service_unavailable': 'unconfirmed'},
                                      'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                                  'analysis_kind_list_missing_selection': 'no_assertion',
                                                                                  'analysis_provider_list_selection_changed': 'no_assertion',
                                                                                  'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                                  'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                                  'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                                  'database_connection_preparation_failed': 'no_assertion',
                                                                                  'database_executor_quarantined': 'no_assertion',
                                                                                  'database_idle_transaction_timeout': 'no_assertion',
                                                                                  'database_lock_unavailable': 'no_assertion',
                                                                                  'database_query_canceled': 'no_assertion',
                                                                                  'database_work_admission_stopped': 'no_assertion',
                                                                                  'database_work_slots_exhausted': 'no_assertion',
                                                                                  'description_update_randomness_unavailable': 'no_assertion',
                                                                                  'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_start_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                                  'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                                  'http_exchange_count_exhausted': 'no_domain_change',
                                                                                  'http_exchange_deadline_exceeded': 'no_assertion',
                                                                                  'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                                  'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                                  'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                                  'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                                  'job_create_randomness_unavailable': 'no_assertion',
                                                                                  'job_create_reference_exhausted': 'no_assertion',
                                                                                  'job_deletion_randomness_unavailable': 'no_assertion',
                                                                                  'job_mutation_clock_behind': 'no_assertion',
                                                                                  'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                                  'job_state_update_randomness_unavailable': 'no_assertion',
                                                                                  'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                                  'project_description_clock_behind': 'no_assertion',
                                                                                  'project_list_details_unavailable': 'no_assertion',
                                                                                  'provider_directory_clock_behind': 'no_assertion',
                                                                                  'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                                  'request_signature_verifier_unavailable': 'no_domain_change',
                                                                                  'service_unavailable': 'no_assertion',
                                                                                  'stored_analysis_result_inconsistent': 'no_assertion',
                                                                                  'upload_capability_clock_behind': 'no_assertion',
                                                                                  'upload_capability_expired_before_commit': 'no_assertion',
                                                                                  'upload_capability_generation_failed': 'no_assertion',
                                                                                  'upload_capability_randomness_unavailable': 'no_assertion',
                                                                                  'upload_create_randomness_unavailable': 'no_assertion',
                                                                                  'upload_create_reference_exhausted': 'no_assertion',
                                                                                  'upload_deletion_clock_behind': 'no_assertion',
                                                                                  'upload_description_clock_behind': 'no_assertion',
                                                                                  'upload_finalize_clock_behind': 'no_assertion',
                                                                                  'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                                  'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                                  'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                                  'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                                  'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                                  'upload_finalize_storage_completed_late': 'no_assertion',
                                                                                  'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                                  'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'execution_attempt_fail': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                      'request_query_not_supported': 'no_assertion'}},
                            401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                'authentication_nonce_reused': 'no_domain_change',
                                                                                'authentication_window_closed': 'no_domain_change',
                                                                                'signature_created_in_future': 'no_domain_change',
                                                                                'signature_expired': 'no_domain_change'}},
                            403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                            404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                            408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                            409: {'urn:nmr-api:problem:operation-conflict': {'deployment_upload_record_limit_reached': 'no_assertion',
                                                                             'deployment_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                             'execution_attempt_completion_after_failure': 'no_domain_change',
                                                                             'execution_attempt_completion_replay_mismatch': 'no_domain_change',
                                                                             'execution_attempt_failure_after_success': 'no_domain_change',
                                                                             'execution_attempt_failure_replay_mismatch': 'no_domain_change',
                                                                             'execution_attempt_outcome_expired': 'no_domain_change',
                                                                             'execution_attempt_progress_regression': 'no_assertion',
                                                                             'execution_attempt_progress_terminal': 'no_assertion',
                                                                             'job_attempt_limit_reached': 'no_domain_change',
                                                                             'job_open_pending_uploads': 'no_assertion',
                                                                             'job_provider_attempt_limit_reached': 'no_domain_change',
                                                                             'job_state_change_cancelled': 'no_assertion',
                                                                             'job_upload_selection_cancelled': 'no_assertion',
                                                                             'job_upload_selection_pending': 'no_assertion',
                                                                             'job_upload_selection_removal_scheduled': 'no_assertion',
                                                                             'job_upload_selection_retention_expired': 'no_assertion',
                                                                             'operation_conflict': 'no_assertion',
                                                                             'operation_reference_conflict': 'no_assertion',
                                                                             'project_job_limit_reached': 'no_assertion',
                                                                             'project_operation_record_limit_reached': 'no_assertion',
                                                                             'project_purge_in_progress': 'no_assertion',
                                                                             'project_upload_record_limit_reached': 'no_assertion',
                                                                             'project_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                             'provider_attempt_key_conflict': 'no_domain_change',
                                                                             'upload_byte_length_limit_exceeded': 'no_assertion',
                                                                             'upload_finalize_bytes_absent': 'no_assertion',
                                                                             'upload_finalize_bytes_incomplete': 'no_assertion',
                                                                             'upload_finalize_removal_scheduled': 'no_assertion',
                                                                             'upload_linked_to_job': 'no_assertion',
                                                                             'upload_publish_already_finalized': 'no_assertion',
                                                                             'upload_publish_removal_scheduled': 'no_assertion',
                                                                             'upload_read_not_finalized': 'no_assertion',
                                                                             'upload_read_removal_scheduled': 'no_assertion'}},
                            413: {'urn:nmr-api:problem:request-content-too-large': {'request_content_too_large': 'no_assertion'}},
                            414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                       'request_query_too_large': 'no_assertion'}},
                            431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                          'request_header_count_too_large': 'no_assertion'}},
                            500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                         'internal_error': 'no_assertion',
                                                                         'job_open_inputs_inconsistent': 'no_assertion',
                                                                         'request_signature_processing_failed': 'no_domain_change',
                                                                         'response_size_limit_exceeded': 'no_assertion',
                                                                         'response_validation_failed': 'no_assertion',
                                                                         'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                         'upload_finalize_length_mismatch': 'no_assertion',
                                                                         'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                         'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                         'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                            503: {'urn:nmr-api:problem:mutation-outcome-unconfirmed': {'http_exchange_deadline_exceeded': 'unconfirmed',
                                                                                       'service_unavailable': 'unconfirmed'},
                                  'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                              'analysis_kind_list_missing_selection': 'no_assertion',
                                                                              'analysis_provider_list_selection_changed': 'no_assertion',
                                                                              'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                              'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                              'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                              'database_connection_preparation_failed': 'no_assertion',
                                                                              'database_executor_quarantined': 'no_assertion',
                                                                              'database_idle_transaction_timeout': 'no_assertion',
                                                                              'database_lock_unavailable': 'no_assertion',
                                                                              'database_query_canceled': 'no_assertion',
                                                                              'database_work_admission_stopped': 'no_assertion',
                                                                              'database_work_slots_exhausted': 'no_assertion',
                                                                              'description_update_randomness_unavailable': 'no_assertion',
                                                                              'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                              'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                              'execution_attempt_start_clock_behind': 'no_assertion',
                                                                              'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                              'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                              'http_exchange_count_exhausted': 'no_domain_change',
                                                                              'http_exchange_deadline_exceeded': 'no_assertion',
                                                                              'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                              'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                              'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                              'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                              'job_create_randomness_unavailable': 'no_assertion',
                                                                              'job_create_reference_exhausted': 'no_assertion',
                                                                              'job_deletion_randomness_unavailable': 'no_assertion',
                                                                              'job_mutation_clock_behind': 'no_assertion',
                                                                              'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                              'job_state_update_randomness_unavailable': 'no_assertion',
                                                                              'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                              'project_description_clock_behind': 'no_assertion',
                                                                              'project_list_details_unavailable': 'no_assertion',
                                                                              'provider_directory_clock_behind': 'no_assertion',
                                                                              'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                              'request_signature_verifier_unavailable': 'no_domain_change',
                                                                              'service_unavailable': 'no_assertion',
                                                                              'stored_analysis_result_inconsistent': 'no_assertion',
                                                                              'upload_capability_clock_behind': 'no_assertion',
                                                                              'upload_capability_expired_before_commit': 'no_assertion',
                                                                              'upload_capability_generation_failed': 'no_assertion',
                                                                              'upload_capability_randomness_unavailable': 'no_assertion',
                                                                              'upload_create_randomness_unavailable': 'no_assertion',
                                                                              'upload_create_reference_exhausted': 'no_assertion',
                                                                              'upload_deletion_clock_behind': 'no_assertion',
                                                                              'upload_description_clock_behind': 'no_assertion',
                                                                              'upload_finalize_clock_behind': 'no_assertion',
                                                                              'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                              'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                              'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                              'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                              'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                              'upload_finalize_storage_completed_late': 'no_assertion',
                                                                              'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                              'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                              'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                              'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                              'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'execution_attempt_progress': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                          'request_query_not_supported': 'no_assertion'}},
                                401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                    'authentication_nonce_reused': 'no_domain_change',
                                                                                    'authentication_window_closed': 'no_domain_change',
                                                                                    'signature_created_in_future': 'no_domain_change',
                                                                                    'signature_expired': 'no_domain_change'}},
                                403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                                404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                                408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                                409: {'urn:nmr-api:problem:operation-conflict': {'deployment_upload_record_limit_reached': 'no_assertion',
                                                                                 'deployment_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                                 'execution_attempt_completion_after_failure': 'no_domain_change',
                                                                                 'execution_attempt_completion_replay_mismatch': 'no_domain_change',
                                                                                 'execution_attempt_failure_after_success': 'no_domain_change',
                                                                                 'execution_attempt_failure_replay_mismatch': 'no_domain_change',
                                                                                 'execution_attempt_outcome_expired': 'no_domain_change',
                                                                                 'execution_attempt_progress_regression': 'no_assertion',
                                                                                 'execution_attempt_progress_terminal': 'no_assertion',
                                                                                 'job_attempt_limit_reached': 'no_domain_change',
                                                                                 'job_open_pending_uploads': 'no_assertion',
                                                                                 'job_provider_attempt_limit_reached': 'no_domain_change',
                                                                                 'job_state_change_cancelled': 'no_assertion',
                                                                                 'job_upload_selection_cancelled': 'no_assertion',
                                                                                 'job_upload_selection_pending': 'no_assertion',
                                                                                 'job_upload_selection_removal_scheduled': 'no_assertion',
                                                                                 'job_upload_selection_retention_expired': 'no_assertion',
                                                                                 'operation_conflict': 'no_assertion',
                                                                                 'operation_reference_conflict': 'no_assertion',
                                                                                 'project_job_limit_reached': 'no_assertion',
                                                                                 'project_operation_record_limit_reached': 'no_assertion',
                                                                                 'project_purge_in_progress': 'no_assertion',
                                                                                 'project_upload_record_limit_reached': 'no_assertion',
                                                                                 'project_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                                 'provider_attempt_key_conflict': 'no_domain_change',
                                                                                 'upload_byte_length_limit_exceeded': 'no_assertion',
                                                                                 'upload_finalize_bytes_absent': 'no_assertion',
                                                                                 'upload_finalize_bytes_incomplete': 'no_assertion',
                                                                                 'upload_finalize_removal_scheduled': 'no_assertion',
                                                                                 'upload_linked_to_job': 'no_assertion',
                                                                                 'upload_publish_already_finalized': 'no_assertion',
                                                                                 'upload_publish_removal_scheduled': 'no_assertion',
                                                                                 'upload_read_not_finalized': 'no_assertion',
                                                                                 'upload_read_removal_scheduled': 'no_assertion'}},
                                413: {'urn:nmr-api:problem:request-content-too-large': {'request_content_too_large': 'no_assertion'}},
                                414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                           'request_query_too_large': 'no_assertion'}},
                                431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                              'request_header_count_too_large': 'no_assertion'}},
                                500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                             'internal_error': 'no_assertion',
                                                                             'job_open_inputs_inconsistent': 'no_assertion',
                                                                             'request_signature_processing_failed': 'no_domain_change',
                                                                             'response_size_limit_exceeded': 'no_assertion',
                                                                             'response_validation_failed': 'no_assertion',
                                                                             'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                             'upload_finalize_length_mismatch': 'no_assertion',
                                                                             'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                             'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                             'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                                503: {'urn:nmr-api:problem:mutation-outcome-unconfirmed': {'http_exchange_deadline_exceeded': 'unconfirmed',
                                                                                           'service_unavailable': 'unconfirmed'},
                                      'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                                  'analysis_kind_list_missing_selection': 'no_assertion',
                                                                                  'analysis_provider_list_selection_changed': 'no_assertion',
                                                                                  'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                                  'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                                  'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                                  'database_connection_preparation_failed': 'no_assertion',
                                                                                  'database_executor_quarantined': 'no_assertion',
                                                                                  'database_idle_transaction_timeout': 'no_assertion',
                                                                                  'database_lock_unavailable': 'no_assertion',
                                                                                  'database_query_canceled': 'no_assertion',
                                                                                  'database_work_admission_stopped': 'no_assertion',
                                                                                  'database_work_slots_exhausted': 'no_assertion',
                                                                                  'description_update_randomness_unavailable': 'no_assertion',
                                                                                  'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_start_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                                  'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                                  'http_exchange_count_exhausted': 'no_domain_change',
                                                                                  'http_exchange_deadline_exceeded': 'no_assertion',
                                                                                  'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                                  'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                                  'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                                  'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                                  'job_create_randomness_unavailable': 'no_assertion',
                                                                                  'job_create_reference_exhausted': 'no_assertion',
                                                                                  'job_deletion_randomness_unavailable': 'no_assertion',
                                                                                  'job_mutation_clock_behind': 'no_assertion',
                                                                                  'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                                  'job_state_update_randomness_unavailable': 'no_assertion',
                                                                                  'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                                  'project_description_clock_behind': 'no_assertion',
                                                                                  'project_list_details_unavailable': 'no_assertion',
                                                                                  'provider_directory_clock_behind': 'no_assertion',
                                                                                  'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                                  'request_signature_verifier_unavailable': 'no_domain_change',
                                                                                  'service_unavailable': 'no_assertion',
                                                                                  'stored_analysis_result_inconsistent': 'no_assertion',
                                                                                  'upload_capability_clock_behind': 'no_assertion',
                                                                                  'upload_capability_expired_before_commit': 'no_assertion',
                                                                                  'upload_capability_generation_failed': 'no_assertion',
                                                                                  'upload_capability_randomness_unavailable': 'no_assertion',
                                                                                  'upload_create_randomness_unavailable': 'no_assertion',
                                                                                  'upload_create_reference_exhausted': 'no_assertion',
                                                                                  'upload_deletion_clock_behind': 'no_assertion',
                                                                                  'upload_description_clock_behind': 'no_assertion',
                                                                                  'upload_finalize_clock_behind': 'no_assertion',
                                                                                  'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                                  'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                                  'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                                  'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                                  'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                                  'upload_finalize_storage_completed_late': 'no_assertion',
                                                                                  'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                                  'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'execution_attempt_read': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                      'request_content_not_supported': 'no_assertion',
                                                                      'request_query_not_supported': 'no_assertion'}},
                            401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                'authentication_nonce_reused': 'no_domain_change',
                                                                                'authentication_window_closed': 'no_domain_change',
                                                                                'signature_created_in_future': 'no_domain_change',
                                                                                'signature_expired': 'no_domain_change'}},
                            403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                            404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                            408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                            414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                       'request_query_too_large': 'no_assertion'}},
                            431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                          'request_header_count_too_large': 'no_assertion'}},
                            500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                         'internal_error': 'no_assertion',
                                                                         'job_open_inputs_inconsistent': 'no_assertion',
                                                                         'request_signature_processing_failed': 'no_domain_change',
                                                                         'response_size_limit_exceeded': 'no_assertion',
                                                                         'response_validation_failed': 'no_assertion',
                                                                         'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                         'upload_finalize_length_mismatch': 'no_assertion',
                                                                         'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                         'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                         'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                         'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                            503: {'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                              'analysis_kind_list_missing_selection': 'no_assertion',
                                                                              'analysis_provider_list_selection_changed': 'no_assertion',
                                                                              'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                              'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                              'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                              'database_connection_preparation_failed': 'no_assertion',
                                                                              'database_executor_quarantined': 'no_assertion',
                                                                              'database_idle_transaction_timeout': 'no_assertion',
                                                                              'database_lock_unavailable': 'no_assertion',
                                                                              'database_query_canceled': 'no_assertion',
                                                                              'database_work_admission_stopped': 'no_assertion',
                                                                              'database_work_slots_exhausted': 'no_assertion',
                                                                              'description_update_randomness_unavailable': 'no_assertion',
                                                                              'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                              'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                              'execution_attempt_start_clock_behind': 'no_assertion',
                                                                              'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                              'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                              'http_exchange_count_exhausted': 'no_domain_change',
                                                                              'http_exchange_deadline_exceeded': 'no_assertion',
                                                                              'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                              'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                              'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                              'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                              'job_create_randomness_unavailable': 'no_assertion',
                                                                              'job_create_reference_exhausted': 'no_assertion',
                                                                              'job_deletion_randomness_unavailable': 'no_assertion',
                                                                              'job_mutation_clock_behind': 'no_assertion',
                                                                              'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                              'job_state_update_randomness_unavailable': 'no_assertion',
                                                                              'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                              'project_description_clock_behind': 'no_assertion',
                                                                              'project_list_details_unavailable': 'no_assertion',
                                                                              'provider_directory_clock_behind': 'no_assertion',
                                                                              'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                              'request_signature_verifier_unavailable': 'no_domain_change',
                                                                              'service_unavailable': 'no_assertion',
                                                                              'stored_analysis_result_inconsistent': 'no_assertion',
                                                                              'upload_capability_clock_behind': 'no_assertion',
                                                                              'upload_capability_expired_before_commit': 'no_assertion',
                                                                              'upload_capability_generation_failed': 'no_assertion',
                                                                              'upload_capability_randomness_unavailable': 'no_assertion',
                                                                              'upload_create_randomness_unavailable': 'no_assertion',
                                                                              'upload_create_reference_exhausted': 'no_assertion',
                                                                              'upload_deletion_clock_behind': 'no_assertion',
                                                                              'upload_description_clock_behind': 'no_assertion',
                                                                              'upload_finalize_clock_behind': 'no_assertion',
                                                                              'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                              'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                              'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                              'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                              'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                              'upload_finalize_storage_completed_late': 'no_assertion',
                                                                              'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                              'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                              'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                              'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                              'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'execution_attempt_start': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                       'request_query_not_supported': 'no_assertion'}},
                             401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                 'authentication_nonce_reused': 'no_domain_change',
                                                                                 'authentication_window_closed': 'no_domain_change',
                                                                                 'signature_created_in_future': 'no_domain_change',
                                                                                 'signature_expired': 'no_domain_change'}},
                             403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                             404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                             408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                             409: {'urn:nmr-api:problem:operation-conflict': {'deployment_upload_record_limit_reached': 'no_assertion',
                                                                              'deployment_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                              'execution_attempt_completion_after_failure': 'no_domain_change',
                                                                              'execution_attempt_completion_replay_mismatch': 'no_domain_change',
                                                                              'execution_attempt_failure_after_success': 'no_domain_change',
                                                                              'execution_attempt_failure_replay_mismatch': 'no_domain_change',
                                                                              'execution_attempt_outcome_expired': 'no_domain_change',
                                                                              'execution_attempt_progress_regression': 'no_assertion',
                                                                              'execution_attempt_progress_terminal': 'no_assertion',
                                                                              'job_attempt_limit_reached': 'no_domain_change',
                                                                              'job_open_pending_uploads': 'no_assertion',
                                                                              'job_provider_attempt_limit_reached': 'no_domain_change',
                                                                              'job_state_change_cancelled': 'no_assertion',
                                                                              'job_upload_selection_cancelled': 'no_assertion',
                                                                              'job_upload_selection_pending': 'no_assertion',
                                                                              'job_upload_selection_removal_scheduled': 'no_assertion',
                                                                              'job_upload_selection_retention_expired': 'no_assertion',
                                                                              'operation_conflict': 'no_assertion',
                                                                              'operation_reference_conflict': 'no_assertion',
                                                                              'project_job_limit_reached': 'no_assertion',
                                                                              'project_operation_record_limit_reached': 'no_assertion',
                                                                              'project_purge_in_progress': 'no_assertion',
                                                                              'project_upload_record_limit_reached': 'no_assertion',
                                                                              'project_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                              'provider_attempt_key_conflict': 'no_domain_change',
                                                                              'upload_byte_length_limit_exceeded': 'no_assertion',
                                                                              'upload_finalize_bytes_absent': 'no_assertion',
                                                                              'upload_finalize_bytes_incomplete': 'no_assertion',
                                                                              'upload_finalize_removal_scheduled': 'no_assertion',
                                                                              'upload_linked_to_job': 'no_assertion',
                                                                              'upload_publish_already_finalized': 'no_assertion',
                                                                              'upload_publish_removal_scheduled': 'no_assertion',
                                                                              'upload_read_not_finalized': 'no_assertion',
                                                                              'upload_read_removal_scheduled': 'no_assertion'}},
                             413: {'urn:nmr-api:problem:request-content-too-large': {'request_content_too_large': 'no_assertion'}},
                             414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                        'request_query_too_large': 'no_assertion'}},
                             431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                           'request_header_count_too_large': 'no_assertion'}},
                             500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                          'internal_error': 'no_assertion',
                                                                          'job_open_inputs_inconsistent': 'no_assertion',
                                                                          'request_signature_processing_failed': 'no_domain_change',
                                                                          'response_size_limit_exceeded': 'no_assertion',
                                                                          'response_validation_failed': 'no_assertion',
                                                                          'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                          'upload_finalize_length_mismatch': 'no_assertion',
                                                                          'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                          'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                          'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                             503: {'urn:nmr-api:problem:mutation-outcome-unconfirmed': {'http_exchange_deadline_exceeded': 'unconfirmed',
                                                                                        'service_unavailable': 'unconfirmed'},
                                   'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                               'analysis_kind_list_missing_selection': 'no_assertion',
                                                                               'analysis_provider_list_selection_changed': 'no_assertion',
                                                                               'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                               'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                               'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                               'database_connection_preparation_failed': 'no_assertion',
                                                                               'database_executor_quarantined': 'no_assertion',
                                                                               'database_idle_transaction_timeout': 'no_assertion',
                                                                               'database_lock_unavailable': 'no_assertion',
                                                                               'database_query_canceled': 'no_assertion',
                                                                               'database_work_admission_stopped': 'no_assertion',
                                                                               'database_work_slots_exhausted': 'no_assertion',
                                                                               'description_update_randomness_unavailable': 'no_assertion',
                                                                               'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                               'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                               'execution_attempt_start_clock_behind': 'no_assertion',
                                                                               'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                               'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                               'http_exchange_count_exhausted': 'no_domain_change',
                                                                               'http_exchange_deadline_exceeded': 'no_assertion',
                                                                               'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                               'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                               'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                               'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                               'job_create_randomness_unavailable': 'no_assertion',
                                                                               'job_create_reference_exhausted': 'no_assertion',
                                                                               'job_deletion_randomness_unavailable': 'no_assertion',
                                                                               'job_mutation_clock_behind': 'no_assertion',
                                                                               'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                               'job_state_update_randomness_unavailable': 'no_assertion',
                                                                               'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                               'project_description_clock_behind': 'no_assertion',
                                                                               'project_list_details_unavailable': 'no_assertion',
                                                                               'provider_directory_clock_behind': 'no_assertion',
                                                                               'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                               'request_signature_verifier_unavailable': 'no_domain_change',
                                                                               'service_unavailable': 'no_assertion',
                                                                               'stored_analysis_result_inconsistent': 'no_assertion',
                                                                               'upload_capability_clock_behind': 'no_assertion',
                                                                               'upload_capability_expired_before_commit': 'no_assertion',
                                                                               'upload_capability_generation_failed': 'no_assertion',
                                                                               'upload_capability_randomness_unavailable': 'no_assertion',
                                                                               'upload_create_randomness_unavailable': 'no_assertion',
                                                                               'upload_create_reference_exhausted': 'no_assertion',
                                                                               'upload_deletion_clock_behind': 'no_assertion',
                                                                               'upload_description_clock_behind': 'no_assertion',
                                                                               'upload_finalize_clock_behind': 'no_assertion',
                                                                               'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                               'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                               'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                               'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                               'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                               'upload_finalize_storage_completed_late': 'no_assertion',
                                                                               'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                               'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                               'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                               'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                               'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'execution_attempts_list': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                       'request_content_not_supported': 'no_assertion'}},
                             401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                 'authentication_nonce_reused': 'no_domain_change',
                                                                                 'authentication_window_closed': 'no_domain_change',
                                                                                 'signature_created_in_future': 'no_domain_change',
                                                                                 'signature_expired': 'no_domain_change'}},
                             403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                             408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                             414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                        'request_query_too_large': 'no_assertion'}},
                             431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                           'request_header_count_too_large': 'no_assertion'}},
                             500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                          'internal_error': 'no_assertion',
                                                                          'job_open_inputs_inconsistent': 'no_assertion',
                                                                          'request_signature_processing_failed': 'no_domain_change',
                                                                          'response_size_limit_exceeded': 'no_assertion',
                                                                          'response_validation_failed': 'no_assertion',
                                                                          'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                          'upload_finalize_length_mismatch': 'no_assertion',
                                                                          'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                          'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                          'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                          'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                             503: {'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                               'analysis_kind_list_missing_selection': 'no_assertion',
                                                                               'analysis_provider_list_selection_changed': 'no_assertion',
                                                                               'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                               'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                               'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                               'database_connection_preparation_failed': 'no_assertion',
                                                                               'database_executor_quarantined': 'no_assertion',
                                                                               'database_idle_transaction_timeout': 'no_assertion',
                                                                               'database_lock_unavailable': 'no_assertion',
                                                                               'database_query_canceled': 'no_assertion',
                                                                               'database_work_admission_stopped': 'no_assertion',
                                                                               'database_work_slots_exhausted': 'no_assertion',
                                                                               'description_update_randomness_unavailable': 'no_assertion',
                                                                               'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                               'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                               'execution_attempt_start_clock_behind': 'no_assertion',
                                                                               'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                               'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                               'http_exchange_count_exhausted': 'no_domain_change',
                                                                               'http_exchange_deadline_exceeded': 'no_assertion',
                                                                               'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                               'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                               'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                               'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                               'job_create_randomness_unavailable': 'no_assertion',
                                                                               'job_create_reference_exhausted': 'no_assertion',
                                                                               'job_deletion_randomness_unavailable': 'no_assertion',
                                                                               'job_mutation_clock_behind': 'no_assertion',
                                                                               'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                               'job_state_update_randomness_unavailable': 'no_assertion',
                                                                               'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                               'project_description_clock_behind': 'no_assertion',
                                                                               'project_list_details_unavailable': 'no_assertion',
                                                                               'provider_directory_clock_behind': 'no_assertion',
                                                                               'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                               'request_signature_verifier_unavailable': 'no_domain_change',
                                                                               'service_unavailable': 'no_assertion',
                                                                               'stored_analysis_result_inconsistent': 'no_assertion',
                                                                               'upload_capability_clock_behind': 'no_assertion',
                                                                               'upload_capability_expired_before_commit': 'no_assertion',
                                                                               'upload_capability_generation_failed': 'no_assertion',
                                                                               'upload_capability_randomness_unavailable': 'no_assertion',
                                                                               'upload_create_randomness_unavailable': 'no_assertion',
                                                                               'upload_create_reference_exhausted': 'no_assertion',
                                                                               'upload_deletion_clock_behind': 'no_assertion',
                                                                               'upload_description_clock_behind': 'no_assertion',
                                                                               'upload_finalize_clock_behind': 'no_assertion',
                                                                               'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                               'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                               'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                               'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                               'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                               'upload_finalize_storage_completed_late': 'no_assertion',
                                                                               'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                               'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                               'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                               'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                               'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'job_input_read': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                              'request_content_not_supported': 'no_assertion'}},
                    401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                        'authentication_nonce_reused': 'no_domain_change',
                                                                        'authentication_window_closed': 'no_domain_change',
                                                                        'signature_created_in_future': 'no_domain_change',
                                                                        'signature_expired': 'no_domain_change'}},
                    403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                    404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                    408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                    414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                               'request_query_too_large': 'no_assertion'}},
                    431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                  'request_header_count_too_large': 'no_assertion'}},
                    500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                 'internal_error': 'no_assertion',
                                                                 'job_open_inputs_inconsistent': 'no_assertion',
                                                                 'request_signature_processing_failed': 'no_domain_change',
                                                                 'response_size_limit_exceeded': 'no_assertion',
                                                                 'response_validation_failed': 'no_assertion',
                                                                 'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                 'upload_finalize_length_mismatch': 'no_assertion',
                                                                 'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                 'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                 'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                    503: {'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                      'analysis_kind_list_missing_selection': 'no_assertion',
                                                                      'analysis_provider_list_selection_changed': 'no_assertion',
                                                                      'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                      'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                      'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                      'database_connection_preparation_failed': 'no_assertion',
                                                                      'database_executor_quarantined': 'no_assertion',
                                                                      'database_idle_transaction_timeout': 'no_assertion',
                                                                      'database_lock_unavailable': 'no_assertion',
                                                                      'database_query_canceled': 'no_assertion',
                                                                      'database_work_admission_stopped': 'no_assertion',
                                                                      'database_work_slots_exhausted': 'no_assertion',
                                                                      'description_update_randomness_unavailable': 'no_assertion',
                                                                      'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                      'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                      'execution_attempt_start_clock_behind': 'no_assertion',
                                                                      'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                      'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                      'http_exchange_count_exhausted': 'no_domain_change',
                                                                      'http_exchange_deadline_exceeded': 'no_assertion',
                                                                      'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                      'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                      'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                      'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                      'job_create_randomness_unavailable': 'no_assertion',
                                                                      'job_create_reference_exhausted': 'no_assertion',
                                                                      'job_deletion_randomness_unavailable': 'no_assertion',
                                                                      'job_mutation_clock_behind': 'no_assertion',
                                                                      'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                      'job_state_update_randomness_unavailable': 'no_assertion',
                                                                      'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                      'project_description_clock_behind': 'no_assertion',
                                                                      'project_list_details_unavailable': 'no_assertion',
                                                                      'provider_directory_clock_behind': 'no_assertion',
                                                                      'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                      'request_signature_verifier_unavailable': 'no_domain_change',
                                                                      'service_unavailable': 'no_assertion',
                                                                      'stored_analysis_result_inconsistent': 'no_assertion',
                                                                      'upload_capability_clock_behind': 'no_assertion',
                                                                      'upload_capability_expired_before_commit': 'no_assertion',
                                                                      'upload_capability_generation_failed': 'no_assertion',
                                                                      'upload_capability_randomness_unavailable': 'no_assertion',
                                                                      'upload_create_randomness_unavailable': 'no_assertion',
                                                                      'upload_create_reference_exhausted': 'no_assertion',
                                                                      'upload_deletion_clock_behind': 'no_assertion',
                                                                      'upload_description_clock_behind': 'no_assertion',
                                                                      'upload_finalize_clock_behind': 'no_assertion',
                                                                      'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                      'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                      'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                      'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                      'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                      'upload_finalize_storage_completed_late': 'no_assertion',
                                                                      'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                      'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                      'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                      'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                      'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'job_upload_read_capability': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                          'request_content_not_supported': 'no_assertion',
                                                                          'request_query_not_supported': 'no_assertion'}},
                                401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                                    'authentication_nonce_reused': 'no_domain_change',
                                                                                    'authentication_window_closed': 'no_domain_change',
                                                                                    'signature_created_in_future': 'no_domain_change',
                                                                                    'signature_expired': 'no_domain_change'}},
                                403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                                404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                                408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                                409: {'urn:nmr-api:problem:operation-conflict': {'deployment_upload_record_limit_reached': 'no_assertion',
                                                                                 'deployment_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                                 'execution_attempt_completion_after_failure': 'no_domain_change',
                                                                                 'execution_attempt_completion_replay_mismatch': 'no_domain_change',
                                                                                 'execution_attempt_failure_after_success': 'no_domain_change',
                                                                                 'execution_attempt_failure_replay_mismatch': 'no_domain_change',
                                                                                 'execution_attempt_outcome_expired': 'no_domain_change',
                                                                                 'execution_attempt_progress_regression': 'no_assertion',
                                                                                 'execution_attempt_progress_terminal': 'no_assertion',
                                                                                 'job_attempt_limit_reached': 'no_domain_change',
                                                                                 'job_open_pending_uploads': 'no_assertion',
                                                                                 'job_provider_attempt_limit_reached': 'no_domain_change',
                                                                                 'job_state_change_cancelled': 'no_assertion',
                                                                                 'job_upload_selection_cancelled': 'no_assertion',
                                                                                 'job_upload_selection_pending': 'no_assertion',
                                                                                 'job_upload_selection_removal_scheduled': 'no_assertion',
                                                                                 'job_upload_selection_retention_expired': 'no_assertion',
                                                                                 'operation_conflict': 'no_assertion',
                                                                                 'operation_reference_conflict': 'no_assertion',
                                                                                 'project_job_limit_reached': 'no_assertion',
                                                                                 'project_operation_record_limit_reached': 'no_assertion',
                                                                                 'project_purge_in_progress': 'no_assertion',
                                                                                 'project_upload_record_limit_reached': 'no_assertion',
                                                                                 'project_upload_reserved_bytes_limit_exceeded': 'no_assertion',
                                                                                 'provider_attempt_key_conflict': 'no_domain_change',
                                                                                 'upload_byte_length_limit_exceeded': 'no_assertion',
                                                                                 'upload_finalize_bytes_absent': 'no_assertion',
                                                                                 'upload_finalize_bytes_incomplete': 'no_assertion',
                                                                                 'upload_finalize_removal_scheduled': 'no_assertion',
                                                                                 'upload_linked_to_job': 'no_assertion',
                                                                                 'upload_publish_already_finalized': 'no_assertion',
                                                                                 'upload_publish_removal_scheduled': 'no_assertion',
                                                                                 'upload_read_not_finalized': 'no_assertion',
                                                                                 'upload_read_removal_scheduled': 'no_assertion'}},
                                414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                           'request_query_too_large': 'no_assertion'}},
                                431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                              'request_header_count_too_large': 'no_assertion'}},
                                500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                             'internal_error': 'no_assertion',
                                                                             'job_open_inputs_inconsistent': 'no_assertion',
                                                                             'request_signature_processing_failed': 'no_domain_change',
                                                                             'response_size_limit_exceeded': 'no_assertion',
                                                                             'response_validation_failed': 'no_assertion',
                                                                             'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                             'upload_finalize_length_mismatch': 'no_assertion',
                                                                             'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                             'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                             'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                             'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                                503: {'urn:nmr-api:problem:mutation-outcome-unconfirmed': {'http_exchange_deadline_exceeded': 'unconfirmed',
                                                                                           'service_unavailable': 'unconfirmed'},
                                      'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                                  'analysis_kind_list_missing_selection': 'no_assertion',
                                                                                  'analysis_provider_list_selection_changed': 'no_assertion',
                                                                                  'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                                  'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                                  'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                                  'database_connection_preparation_failed': 'no_assertion',
                                                                                  'database_executor_quarantined': 'no_assertion',
                                                                                  'database_idle_transaction_timeout': 'no_assertion',
                                                                                  'database_lock_unavailable': 'no_assertion',
                                                                                  'database_query_canceled': 'no_assertion',
                                                                                  'database_work_admission_stopped': 'no_assertion',
                                                                                  'database_work_slots_exhausted': 'no_assertion',
                                                                                  'description_update_randomness_unavailable': 'no_assertion',
                                                                                  'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_start_clock_behind': 'no_assertion',
                                                                                  'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                                  'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                                  'http_exchange_count_exhausted': 'no_domain_change',
                                                                                  'http_exchange_deadline_exceeded': 'no_assertion',
                                                                                  'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                                  'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                                  'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                                  'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                                  'job_create_randomness_unavailable': 'no_assertion',
                                                                                  'job_create_reference_exhausted': 'no_assertion',
                                                                                  'job_deletion_randomness_unavailable': 'no_assertion',
                                                                                  'job_mutation_clock_behind': 'no_assertion',
                                                                                  'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                                  'job_state_update_randomness_unavailable': 'no_assertion',
                                                                                  'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                                  'project_description_clock_behind': 'no_assertion',
                                                                                  'project_list_details_unavailable': 'no_assertion',
                                                                                  'provider_directory_clock_behind': 'no_assertion',
                                                                                  'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                                  'request_signature_verifier_unavailable': 'no_domain_change',
                                                                                  'service_unavailable': 'no_assertion',
                                                                                  'stored_analysis_result_inconsistent': 'no_assertion',
                                                                                  'upload_capability_clock_behind': 'no_assertion',
                                                                                  'upload_capability_expired_before_commit': 'no_assertion',
                                                                                  'upload_capability_generation_failed': 'no_assertion',
                                                                                  'upload_capability_randomness_unavailable': 'no_assertion',
                                                                                  'upload_create_randomness_unavailable': 'no_assertion',
                                                                                  'upload_create_reference_exhausted': 'no_assertion',
                                                                                  'upload_deletion_clock_behind': 'no_assertion',
                                                                                  'upload_description_clock_behind': 'no_assertion',
                                                                                  'upload_finalize_clock_behind': 'no_assertion',
                                                                                  'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                                  'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                                  'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                                  'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                                  'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                                  'upload_finalize_storage_completed_late': 'no_assertion',
                                                                                  'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                                  'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                                  'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'job_upload_set_read': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                                   'request_content_not_supported': 'no_assertion',
                                                                   'request_query_not_supported': 'no_assertion'}},
                         401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                             'authentication_nonce_reused': 'no_domain_change',
                                                                             'authentication_window_closed': 'no_domain_change',
                                                                             'signature_created_in_future': 'no_domain_change',
                                                                             'signature_expired': 'no_domain_change'}},
                         403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                         404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                         408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                         414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                                    'request_query_too_large': 'no_assertion'}},
                         431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                       'request_header_count_too_large': 'no_assertion'}},
                         500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                      'internal_error': 'no_assertion',
                                                                      'job_open_inputs_inconsistent': 'no_assertion',
                                                                      'request_signature_processing_failed': 'no_domain_change',
                                                                      'response_size_limit_exceeded': 'no_assertion',
                                                                      'response_validation_failed': 'no_assertion',
                                                                      'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                      'upload_finalize_length_mismatch': 'no_assertion',
                                                                      'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                      'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                      'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                      'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                      'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                      'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                      'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                         503: {'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                           'analysis_kind_list_missing_selection': 'no_assertion',
                                                                           'analysis_provider_list_selection_changed': 'no_assertion',
                                                                           'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                           'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                           'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                           'database_connection_preparation_failed': 'no_assertion',
                                                                           'database_executor_quarantined': 'no_assertion',
                                                                           'database_idle_transaction_timeout': 'no_assertion',
                                                                           'database_lock_unavailable': 'no_assertion',
                                                                           'database_query_canceled': 'no_assertion',
                                                                           'database_work_admission_stopped': 'no_assertion',
                                                                           'database_work_slots_exhausted': 'no_assertion',
                                                                           'description_update_randomness_unavailable': 'no_assertion',
                                                                           'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                           'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                           'execution_attempt_start_clock_behind': 'no_assertion',
                                                                           'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                           'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                           'http_exchange_count_exhausted': 'no_domain_change',
                                                                           'http_exchange_deadline_exceeded': 'no_assertion',
                                                                           'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                           'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                           'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                           'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                           'job_create_randomness_unavailable': 'no_assertion',
                                                                           'job_create_reference_exhausted': 'no_assertion',
                                                                           'job_deletion_randomness_unavailable': 'no_assertion',
                                                                           'job_mutation_clock_behind': 'no_assertion',
                                                                           'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                           'job_state_update_randomness_unavailable': 'no_assertion',
                                                                           'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                           'project_description_clock_behind': 'no_assertion',
                                                                           'project_list_details_unavailable': 'no_assertion',
                                                                           'provider_directory_clock_behind': 'no_assertion',
                                                                           'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                           'request_signature_verifier_unavailable': 'no_domain_change',
                                                                           'service_unavailable': 'no_assertion',
                                                                           'stored_analysis_result_inconsistent': 'no_assertion',
                                                                           'upload_capability_clock_behind': 'no_assertion',
                                                                           'upload_capability_expired_before_commit': 'no_assertion',
                                                                           'upload_capability_generation_failed': 'no_assertion',
                                                                           'upload_capability_randomness_unavailable': 'no_assertion',
                                                                           'upload_create_randomness_unavailable': 'no_assertion',
                                                                           'upload_create_reference_exhausted': 'no_assertion',
                                                                           'upload_deletion_clock_behind': 'no_assertion',
                                                                           'upload_description_clock_behind': 'no_assertion',
                                                                           'upload_finalize_clock_behind': 'no_assertion',
                                                                           'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                           'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                           'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                           'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                           'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                           'upload_finalize_storage_completed_late': 'no_assertion',
                                                                           'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                           'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                           'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                           'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                           'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'jobs_list': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                         'request_content_not_supported': 'no_assertion'}},
               401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                   'authentication_nonce_reused': 'no_domain_change',
                                                                   'authentication_window_closed': 'no_domain_change',
                                                                   'signature_created_in_future': 'no_domain_change',
                                                                   'signature_expired': 'no_domain_change'}},
               403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
               408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
               414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                          'request_query_too_large': 'no_assertion'}},
               431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                             'request_header_count_too_large': 'no_assertion'}},
               500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                            'internal_error': 'no_assertion',
                                                            'job_open_inputs_inconsistent': 'no_assertion',
                                                            'request_signature_processing_failed': 'no_domain_change',
                                                            'response_size_limit_exceeded': 'no_assertion',
                                                            'response_validation_failed': 'no_assertion',
                                                            'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                            'upload_finalize_length_mismatch': 'no_assertion',
                                                            'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                            'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                            'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                            'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                            'upload_finalize_storage_request_invalid': 'no_assertion',
                                                            'upload_finalize_storage_response_invalid': 'no_assertion',
                                                            'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
               503: {'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                 'analysis_kind_list_missing_selection': 'no_assertion',
                                                                 'analysis_provider_list_selection_changed': 'no_assertion',
                                                                 'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                 'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                 'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                 'database_connection_preparation_failed': 'no_assertion',
                                                                 'database_executor_quarantined': 'no_assertion',
                                                                 'database_idle_transaction_timeout': 'no_assertion',
                                                                 'database_lock_unavailable': 'no_assertion',
                                                                 'database_query_canceled': 'no_assertion',
                                                                 'database_work_admission_stopped': 'no_assertion',
                                                                 'database_work_slots_exhausted': 'no_assertion',
                                                                 'description_update_randomness_unavailable': 'no_assertion',
                                                                 'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                 'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                 'execution_attempt_start_clock_behind': 'no_assertion',
                                                                 'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                 'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                 'http_exchange_count_exhausted': 'no_domain_change',
                                                                 'http_exchange_deadline_exceeded': 'no_assertion',
                                                                 'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                 'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                 'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                 'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                 'job_create_randomness_unavailable': 'no_assertion',
                                                                 'job_create_reference_exhausted': 'no_assertion',
                                                                 'job_deletion_randomness_unavailable': 'no_assertion',
                                                                 'job_mutation_clock_behind': 'no_assertion',
                                                                 'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                 'job_state_update_randomness_unavailable': 'no_assertion',
                                                                 'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                 'project_description_clock_behind': 'no_assertion',
                                                                 'project_list_details_unavailable': 'no_assertion',
                                                                 'provider_directory_clock_behind': 'no_assertion',
                                                                 'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                 'request_signature_verifier_unavailable': 'no_domain_change',
                                                                 'service_unavailable': 'no_assertion',
                                                                 'stored_analysis_result_inconsistent': 'no_assertion',
                                                                 'upload_capability_clock_behind': 'no_assertion',
                                                                 'upload_capability_expired_before_commit': 'no_assertion',
                                                                 'upload_capability_generation_failed': 'no_assertion',
                                                                 'upload_capability_randomness_unavailable': 'no_assertion',
                                                                 'upload_create_randomness_unavailable': 'no_assertion',
                                                                 'upload_create_reference_exhausted': 'no_assertion',
                                                                 'upload_deletion_clock_behind': 'no_assertion',
                                                                 'upload_description_clock_behind': 'no_assertion',
                                                                 'upload_finalize_clock_behind': 'no_assertion',
                                                                 'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                 'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                 'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                 'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                 'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                 'upload_finalize_storage_completed_late': 'no_assertion',
                                                                 'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                 'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                 'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                 'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                 'upload_finalize_storage_slots_exhausted': 'no_assertion'}}},
 'provider_hello': {400: {'urn:nmr-api:problem:bad-request': {'provider_request_invalid': 'no_assertion',
                                                              'request_query_not_supported': 'no_assertion'}},
                    401: {'urn:nmr-api:problem:authentication-failed': {'authentication_failed': 'no_domain_change',
                                                                        'authentication_nonce_reused': 'no_domain_change',
                                                                        'authentication_window_closed': 'no_domain_change',
                                                                        'signature_created_in_future': 'no_domain_change',
                                                                        'signature_expired': 'no_domain_change'}},
                    403: {'urn:nmr-api:problem:authorization-denied': {'authorization_denied': 'no_assertion'}},
                    404: {'urn:nmr-api:problem:not-found': {'resource_not_found': 'no_assertion'}},
                    408: {'urn:nmr-api:problem:request-body-timeout': {'request_body_timeout': 'no_domain_change'}},
                    413: {'urn:nmr-api:problem:request-content-too-large': {'request_content_too_large': 'no_assertion'}},
                    414: {'urn:nmr-api:problem:uri-too-long': {'request_path_too_large': 'no_assertion',
                                                               'request_query_too_large': 'no_assertion'}},
                    431: {'urn:nmr-api:problem:request-header-fields-too-large': {'request_header_bytes_too_large': 'no_assertion',
                                                                                  'request_header_count_too_large': 'no_assertion'}},
                    500: {'urn:nmr-api:problem:internal-error': {'http_route_memory_budget_too_small': 'no_domain_change',
                                                                 'internal_error': 'no_assertion',
                                                                 'job_open_inputs_inconsistent': 'no_assertion',
                                                                 'request_signature_processing_failed': 'no_domain_change',
                                                                 'response_size_limit_exceeded': 'no_assertion',
                                                                 'response_validation_failed': 'no_assertion',
                                                                 'upload_finalize_committed_facts_mismatch': 'no_assertion',
                                                                 'upload_finalize_length_mismatch': 'no_assertion',
                                                                 'upload_finalize_storage_error_contract_mismatch': 'no_assertion',
                                                                 'upload_finalize_storage_error_fields_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_error_json_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_integrity_failed': 'no_assertion',
                                                                 'upload_finalize_storage_request_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_response_invalid': 'no_assertion',
                                                                 'upload_finalize_storage_token_creation_failed': 'no_assertion'}},
                    503: {'urn:nmr-api:problem:mutation-outcome-unconfirmed': {'http_exchange_deadline_exceeded': 'unconfirmed',
                                                                               'service_unavailable': 'unconfirmed'},
                          'urn:nmr-api:problem:service-unavailable': {'analysis_kind_list_inactive_selection': 'no_assertion',
                                                                      'analysis_kind_list_missing_selection': 'no_assertion',
                                                                      'analysis_provider_list_selection_changed': 'no_assertion',
                                                                      'analysis_provider_list_selection_unavailable': 'no_assertion',
                                                                      'authentication_replay_capacity_exhausted': 'no_domain_change',
                                                                      'authentication_replay_ledger_unavailable': 'no_domain_change',
                                                                      'database_connection_preparation_failed': 'no_assertion',
                                                                      'database_executor_quarantined': 'no_assertion',
                                                                      'database_idle_transaction_timeout': 'no_assertion',
                                                                      'database_lock_unavailable': 'no_assertion',
                                                                      'database_query_canceled': 'no_assertion',
                                                                      'database_work_admission_stopped': 'no_assertion',
                                                                      'database_work_slots_exhausted': 'no_assertion',
                                                                      'description_update_randomness_unavailable': 'no_assertion',
                                                                      'execution_attempt_outcome_clock_behind': 'no_assertion',
                                                                      'execution_attempt_progress_clock_behind': 'no_assertion',
                                                                      'execution_attempt_start_clock_behind': 'no_assertion',
                                                                      'execution_attempt_start_randomness_unavailable': 'no_assertion',
                                                                      'execution_attempt_start_reference_exhausted': 'no_assertion',
                                                                      'http_exchange_count_exhausted': 'no_domain_change',
                                                                      'http_exchange_deadline_exceeded': 'no_assertion',
                                                                      'http_exchange_memory_budget_exhausted': 'no_domain_change',
                                                                      'http_exchange_runtime_not_accepting': 'no_domain_change',
                                                                      'http_route_memory_capacity_exhausted': 'no_domain_change',
                                                                      'job_cancellation_randomness_unavailable': 'no_assertion',
                                                                      'job_create_randomness_unavailable': 'no_assertion',
                                                                      'job_create_reference_exhausted': 'no_assertion',
                                                                      'job_deletion_randomness_unavailable': 'no_assertion',
                                                                      'job_mutation_clock_behind': 'no_assertion',
                                                                      'job_provider_selection_randomness_unavailable': 'no_assertion',
                                                                      'job_state_update_randomness_unavailable': 'no_assertion',
                                                                      'job_upload_set_randomness_unavailable': 'no_assertion',
                                                                      'project_description_clock_behind': 'no_assertion',
                                                                      'project_list_details_unavailable': 'no_assertion',
                                                                      'provider_directory_clock_behind': 'no_assertion',
                                                                      'provider_execution_attempt_capacity_exhausted': 'no_domain_change',
                                                                      'request_signature_verifier_unavailable': 'no_domain_change',
                                                                      'service_unavailable': 'no_assertion',
                                                                      'stored_analysis_result_inconsistent': 'no_assertion',
                                                                      'upload_capability_clock_behind': 'no_assertion',
                                                                      'upload_capability_expired_before_commit': 'no_assertion',
                                                                      'upload_capability_generation_failed': 'no_assertion',
                                                                      'upload_capability_randomness_unavailable': 'no_assertion',
                                                                      'upload_create_randomness_unavailable': 'no_assertion',
                                                                      'upload_create_reference_exhausted': 'no_assertion',
                                                                      'upload_deletion_clock_behind': 'no_assertion',
                                                                      'upload_description_clock_behind': 'no_assertion',
                                                                      'upload_finalize_clock_behind': 'no_assertion',
                                                                      'upload_finalize_randomness_unavailable': 'no_assertion',
                                                                      'upload_finalize_storage_admission_stopped': 'no_assertion',
                                                                      'upload_finalize_storage_authorization_rejected': 'no_assertion',
                                                                      'upload_finalize_storage_cleanup_unconfirmed': 'no_assertion',
                                                                      'upload_finalize_storage_communication_failed': 'no_assertion',
                                                                      'upload_finalize_storage_completed_late': 'no_assertion',
                                                                      'upload_finalize_storage_connection_timeout': 'no_assertion',
                                                                      'upload_finalize_storage_deadline_exceeded': 'no_assertion',
                                                                      'upload_finalize_storage_request_idle_timeout': 'no_assertion',
                                                                      'upload_finalize_storage_request_timeout': 'no_assertion',
                                                                      'upload_finalize_storage_slots_exhausted': 'no_assertion'}}}}
CONFLICT_RECOVERY = {'execution_attempt_complete': {'deployment_upload_record_limit_reached': {'action': 'reconcile_state',
                                                                           'description': 'For '
                                                                                          'other '
                                                                                          'conflicts, '
                                                                                          'reconcile '
                                                                                          'current '
                                                                                          'state '
                                                                                          'and '
                                                                                          'durable '
                                                                                          'replay '
                                                                                          'facts; '
                                                                                          'retry '
                                                                                          'only an '
                                                                                          'exact '
                                                                                          'replay.'},
                                'deployment_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state',
                                                                                    'description': 'For '
                                                                                                   'other '
                                                                                                   'conflicts, '
                                                                                                   'reconcile '
                                                                                                   'current '
                                                                                                   'state '
                                                                                                   'and '
                                                                                                   'durable '
                                                                                                   'replay '
                                                                                                   'facts; '
                                                                                                   'retry '
                                                                                                   'only '
                                                                                                   'an '
                                                                                                   'exact '
                                                                                                   'replay.'},
                                'execution_attempt_completion_after_failure': {'action': 'do_not_resend',
                                                                               'description': 'For '
                                                                                              'execution_attempt_completion_after_failure, '
                                                                                              'do '
                                                                                              'not '
                                                                                              'resend '
                                                                                              'completion; '
                                                                                              'only '
                                                                                              'the '
                                                                                              'original '
                                                                                              'failure '
                                                                                              'report '
                                                                                              'can '
                                                                                              'be '
                                                                                              'replayed.'},
                                'execution_attempt_completion_replay_mismatch': {'action': 'reconcile_original',
                                                                                 'description': 'For '
                                                                                                'execution_attempt_completion_replay_mismatch, '
                                                                                                'only '
                                                                                                'the '
                                                                                                'original '
                                                                                                'unchanged '
                                                                                                'completion '
                                                                                                'request '
                                                                                                'can '
                                                                                                'be '
                                                                                                'replayed; '
                                                                                                'detail '
                                                                                                'identifies '
                                                                                                'changed '
                                                                                                'fields '
                                                                                                'without '
                                                                                                'values.'},
                                'execution_attempt_failure_after_success': {'action': 'do_not_resend',
                                                                            'description': 'For '
                                                                                           'execution_attempt_failure_after_success, '
                                                                                           'do not '
                                                                                           'resend '
                                                                                           'failure; '
                                                                                           'only '
                                                                                           'the '
                                                                                           'original '
                                                                                           'completion '
                                                                                           'request '
                                                                                           'can be '
                                                                                           'replayed.'},
                                'execution_attempt_failure_replay_mismatch': {'action': 'reconcile_original',
                                                                              'description': 'For '
                                                                                             'execution_attempt_failure_replay_mismatch, '
                                                                                             'only '
                                                                                             'the '
                                                                                             'original '
                                                                                             'unchanged '
                                                                                             'failure '
                                                                                             'report '
                                                                                             'can '
                                                                                             'be '
                                                                                             'replayed; '
                                                                                             'detail '
                                                                                             'identifies '
                                                                                             'changed '
                                                                                             'fields '
                                                                                             'without '
                                                                                             'values.'},
                                'execution_attempt_outcome_expired': {'action': 'do_not_resend',
                                                                      'description': 'For '
                                                                                     'execution_attempt_outcome_expired, '
                                                                                     'follow '
                                                                                     'detail; do '
                                                                                     'not resend '
                                                                                     'completion '
                                                                                     'or failure '
                                                                                     'to the '
                                                                                     'expired '
                                                                                     'Attempt.'},
                                'execution_attempt_progress_regression': {'action': 'reconcile_state',
                                                                          'description': 'For '
                                                                                         'other '
                                                                                         'conflicts, '
                                                                                         'reconcile '
                                                                                         'current '
                                                                                         'state '
                                                                                         'and '
                                                                                         'durable '
                                                                                         'replay '
                                                                                         'facts; '
                                                                                         'retry '
                                                                                         'only an '
                                                                                         'exact '
                                                                                         'replay.'},
                                'execution_attempt_progress_terminal': {'action': 'reconcile_state',
                                                                        'description': 'For other '
                                                                                       'conflicts, '
                                                                                       'reconcile '
                                                                                       'current '
                                                                                       'state and '
                                                                                       'durable '
                                                                                       'replay '
                                                                                       'facts; '
                                                                                       'retry only '
                                                                                       'an exact '
                                                                                       'replay.'},
                                'job_attempt_limit_reached': {'action': 'reconcile_state',
                                                              'description': 'For other conflicts, '
                                                                             'reconcile current '
                                                                             'state and durable '
                                                                             'replay facts; retry '
                                                                             'only an exact '
                                                                             'replay.'},
                                'job_open_pending_uploads': {'action': 'reconcile_state',
                                                             'description': 'For other conflicts, '
                                                                            'reconcile current '
                                                                            'state and durable '
                                                                            'replay facts; retry '
                                                                            'only an exact '
                                                                            'replay.'},
                                'job_provider_attempt_limit_reached': {'action': 'reconcile_state',
                                                                       'description': 'For other '
                                                                                      'conflicts, '
                                                                                      'reconcile '
                                                                                      'current '
                                                                                      'state and '
                                                                                      'durable '
                                                                                      'replay '
                                                                                      'facts; '
                                                                                      'retry only '
                                                                                      'an exact '
                                                                                      'replay.'},
                                'job_state_change_cancelled': {'action': 'reconcile_state',
                                                               'description': 'For other '
                                                                              'conflicts, '
                                                                              'reconcile current '
                                                                              'state and durable '
                                                                              'replay facts; retry '
                                                                              'only an exact '
                                                                              'replay.'},
                                'job_upload_selection_cancelled': {'action': 'reconcile_state',
                                                                   'description': 'For other '
                                                                                  'conflicts, '
                                                                                  'reconcile '
                                                                                  'current state '
                                                                                  'and durable '
                                                                                  'replay facts; '
                                                                                  'retry only an '
                                                                                  'exact replay.'},
                                'job_upload_selection_pending': {'action': 'reconcile_state',
                                                                 'description': 'For other '
                                                                                'conflicts, '
                                                                                'reconcile current '
                                                                                'state and durable '
                                                                                'replay facts; '
                                                                                'retry only an '
                                                                                'exact replay.'},
                                'job_upload_selection_removal_scheduled': {'action': 'reconcile_state',
                                                                           'description': 'For '
                                                                                          'other '
                                                                                          'conflicts, '
                                                                                          'reconcile '
                                                                                          'current '
                                                                                          'state '
                                                                                          'and '
                                                                                          'durable '
                                                                                          'replay '
                                                                                          'facts; '
                                                                                          'retry '
                                                                                          'only an '
                                                                                          'exact '
                                                                                          'replay.'},
                                'job_upload_selection_retention_expired': {'action': 'reconcile_state',
                                                                           'description': 'For '
                                                                                          'other '
                                                                                          'conflicts, '
                                                                                          'reconcile '
                                                                                          'current '
                                                                                          'state '
                                                                                          'and '
                                                                                          'durable '
                                                                                          'replay '
                                                                                          'facts; '
                                                                                          'retry '
                                                                                          'only an '
                                                                                          'exact '
                                                                                          'replay.'},
                                'operation_conflict': {'action': 'reconcile_state',
                                                       'description': 'For other conflicts, '
                                                                      'reconcile current state and '
                                                                      'durable replay facts; retry '
                                                                      'only an exact replay.'},
                                'operation_reference_conflict': {'action': 'reconcile_state',
                                                                 'description': 'For other '
                                                                                'conflicts, '
                                                                                'reconcile current '
                                                                                'state and durable '
                                                                                'replay facts; '
                                                                                'retry only an '
                                                                                'exact replay.'},
                                'project_job_limit_reached': {'action': 'reconcile_state',
                                                              'description': 'For other conflicts, '
                                                                             'reconcile current '
                                                                             'state and durable '
                                                                             'replay facts; retry '
                                                                             'only an exact '
                                                                             'replay.'},
                                'project_operation_record_limit_reached': {'action': 'reconcile_state',
                                                                           'description': 'For '
                                                                                          'other '
                                                                                          'conflicts, '
                                                                                          'reconcile '
                                                                                          'current '
                                                                                          'state '
                                                                                          'and '
                                                                                          'durable '
                                                                                          'replay '
                                                                                          'facts; '
                                                                                          'retry '
                                                                                          'only an '
                                                                                          'exact '
                                                                                          'replay.'},
                                'project_purge_in_progress': {'action': 'reconcile_state',
                                                              'description': 'For other conflicts, '
                                                                             'reconcile current '
                                                                             'state and durable '
                                                                             'replay facts; retry '
                                                                             'only an exact '
                                                                             'replay.'},
                                'project_upload_record_limit_reached': {'action': 'reconcile_state',
                                                                        'description': 'For other '
                                                                                       'conflicts, '
                                                                                       'reconcile '
                                                                                       'current '
                                                                                       'state and '
                                                                                       'durable '
                                                                                       'replay '
                                                                                       'facts; '
                                                                                       'retry only '
                                                                                       'an exact '
                                                                                       'replay.'},
                                'project_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state',
                                                                                 'description': 'For '
                                                                                                'other '
                                                                                                'conflicts, '
                                                                                                'reconcile '
                                                                                                'current '
                                                                                                'state '
                                                                                                'and '
                                                                                                'durable '
                                                                                                'replay '
                                                                                                'facts; '
                                                                                                'retry '
                                                                                                'only '
                                                                                                'an '
                                                                                                'exact '
                                                                                                'replay.'},
                                'provider_attempt_key_conflict': {'action': 'reconcile_state',
                                                                  'description': 'For other '
                                                                                 'conflicts, '
                                                                                 'reconcile '
                                                                                 'current state '
                                                                                 'and durable '
                                                                                 'replay facts; '
                                                                                 'retry only an '
                                                                                 'exact replay.'},
                                'upload_byte_length_limit_exceeded': {'action': 'reconcile_state',
                                                                      'description': 'For other '
                                                                                     'conflicts, '
                                                                                     'reconcile '
                                                                                     'current '
                                                                                     'state and '
                                                                                     'durable '
                                                                                     'replay '
                                                                                     'facts; retry '
                                                                                     'only an '
                                                                                     'exact '
                                                                                     'replay.'},
                                'upload_finalize_bytes_absent': {'action': 'reconcile_state',
                                                                 'description': 'For other '
                                                                                'conflicts, '
                                                                                'reconcile current '
                                                                                'state and durable '
                                                                                'replay facts; '
                                                                                'retry only an '
                                                                                'exact replay.'},
                                'upload_finalize_bytes_incomplete': {'action': 'reconcile_state',
                                                                     'description': 'For other '
                                                                                    'conflicts, '
                                                                                    'reconcile '
                                                                                    'current state '
                                                                                    'and durable '
                                                                                    'replay facts; '
                                                                                    'retry only an '
                                                                                    'exact '
                                                                                    'replay.'},
                                'upload_finalize_removal_scheduled': {'action': 'reconcile_state',
                                                                      'description': 'For other '
                                                                                     'conflicts, '
                                                                                     'reconcile '
                                                                                     'current '
                                                                                     'state and '
                                                                                     'durable '
                                                                                     'replay '
                                                                                     'facts; retry '
                                                                                     'only an '
                                                                                     'exact '
                                                                                     'replay.'},
                                'upload_linked_to_job': {'action': 'reconcile_state',
                                                         'description': 'For other conflicts, '
                                                                        'reconcile current state '
                                                                        'and durable replay facts; '
                                                                        'retry only an exact '
                                                                        'replay.'},
                                'upload_publish_already_finalized': {'action': 'reconcile_state',
                                                                     'description': 'For other '
                                                                                    'conflicts, '
                                                                                    'reconcile '
                                                                                    'current state '
                                                                                    'and durable '
                                                                                    'replay facts; '
                                                                                    'retry only an '
                                                                                    'exact '
                                                                                    'replay.'},
                                'upload_publish_removal_scheduled': {'action': 'reconcile_state',
                                                                     'description': 'For other '
                                                                                    'conflicts, '
                                                                                    'reconcile '
                                                                                    'current state '
                                                                                    'and durable '
                                                                                    'replay facts; '
                                                                                    'retry only an '
                                                                                    'exact '
                                                                                    'replay.'},
                                'upload_read_not_finalized': {'action': 'reconcile_state',
                                                              'description': 'For other conflicts, '
                                                                             'reconcile current '
                                                                             'state and durable '
                                                                             'replay facts; retry '
                                                                             'only an exact '
                                                                             'replay.'},
                                'upload_read_removal_scheduled': {'action': 'reconcile_state',
                                                                  'description': 'For other '
                                                                                 'conflicts, '
                                                                                 'reconcile '
                                                                                 'current state '
                                                                                 'and durable '
                                                                                 'replay facts; '
                                                                                 'retry only an '
                                                                                 'exact replay.'}},
 'execution_attempt_fail': {'deployment_upload_record_limit_reached': {'action': 'reconcile_state',
                                                                       'description': 'For other '
                                                                                      'conflicts, '
                                                                                      'reconcile '
                                                                                      'current '
                                                                                      'state and '
                                                                                      'durable '
                                                                                      'replay '
                                                                                      'facts; '
                                                                                      'retry only '
                                                                                      'an exact '
                                                                                      'replay.'},
                            'deployment_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state',
                                                                                'description': 'For '
                                                                                               'other '
                                                                                               'conflicts, '
                                                                                               'reconcile '
                                                                                               'current '
                                                                                               'state '
                                                                                               'and '
                                                                                               'durable '
                                                                                               'replay '
                                                                                               'facts; '
                                                                                               'retry '
                                                                                               'only '
                                                                                               'an '
                                                                                               'exact '
                                                                                               'replay.'},
                            'execution_attempt_completion_after_failure': {'action': 'do_not_resend',
                                                                           'description': 'For '
                                                                                          'execution_attempt_completion_after_failure, '
                                                                                          'do not '
                                                                                          'resend '
                                                                                          'completion; '
                                                                                          'only '
                                                                                          'the '
                                                                                          'original '
                                                                                          'failure '
                                                                                          'report '
                                                                                          'can be '
                                                                                          'replayed.'},
                            'execution_attempt_completion_replay_mismatch': {'action': 'reconcile_original',
                                                                             'description': 'For '
                                                                                            'execution_attempt_completion_replay_mismatch, '
                                                                                            'only '
                                                                                            'the '
                                                                                            'original '
                                                                                            'unchanged '
                                                                                            'completion '
                                                                                            'request '
                                                                                            'can '
                                                                                            'be '
                                                                                            'replayed; '
                                                                                            'detail '
                                                                                            'identifies '
                                                                                            'changed '
                                                                                            'fields '
                                                                                            'without '
                                                                                            'values.'},
                            'execution_attempt_failure_after_success': {'action': 'do_not_resend',
                                                                        'description': 'For '
                                                                                       'execution_attempt_failure_after_success, '
                                                                                       'do not '
                                                                                       'resend '
                                                                                       'failure; '
                                                                                       'only the '
                                                                                       'original '
                                                                                       'completion '
                                                                                       'request '
                                                                                       'can be '
                                                                                       'replayed.'},
                            'execution_attempt_failure_replay_mismatch': {'action': 'reconcile_original',
                                                                          'description': 'For '
                                                                                         'execution_attempt_failure_replay_mismatch, '
                                                                                         'only the '
                                                                                         'original '
                                                                                         'unchanged '
                                                                                         'failure '
                                                                                         'report '
                                                                                         'can be '
                                                                                         'replayed; '
                                                                                         'detail '
                                                                                         'identifies '
                                                                                         'changed '
                                                                                         'fields '
                                                                                         'without '
                                                                                         'values.'},
                            'execution_attempt_outcome_expired': {'action': 'do_not_resend',
                                                                  'description': 'For '
                                                                                 'execution_attempt_outcome_expired, '
                                                                                 'follow detail; '
                                                                                 'do not resend '
                                                                                 'completion or '
                                                                                 'failure to the '
                                                                                 'expired '
                                                                                 'Attempt.'},
                            'execution_attempt_progress_regression': {'action': 'reconcile_state',
                                                                      'description': 'For other '
                                                                                     'conflicts, '
                                                                                     'reconcile '
                                                                                     'current '
                                                                                     'state and '
                                                                                     'durable '
                                                                                     'replay '
                                                                                     'facts; retry '
                                                                                     'only an '
                                                                                     'exact '
                                                                                     'replay.'},
                            'execution_attempt_progress_terminal': {'action': 'reconcile_state',
                                                                    'description': 'For other '
                                                                                   'conflicts, '
                                                                                   'reconcile '
                                                                                   'current state '
                                                                                   'and durable '
                                                                                   'replay facts; '
                                                                                   'retry only an '
                                                                                   'exact replay.'},
                            'job_attempt_limit_reached': {'action': 'reconcile_state',
                                                          'description': 'For other conflicts, '
                                                                         'reconcile current state '
                                                                         'and durable replay '
                                                                         'facts; retry only an '
                                                                         'exact replay.'},
                            'job_open_pending_uploads': {'action': 'reconcile_state',
                                                         'description': 'For other conflicts, '
                                                                        'reconcile current state '
                                                                        'and durable replay facts; '
                                                                        'retry only an exact '
                                                                        'replay.'},
                            'job_provider_attempt_limit_reached': {'action': 'reconcile_state',
                                                                   'description': 'For other '
                                                                                  'conflicts, '
                                                                                  'reconcile '
                                                                                  'current state '
                                                                                  'and durable '
                                                                                  'replay facts; '
                                                                                  'retry only an '
                                                                                  'exact replay.'},
                            'job_state_change_cancelled': {'action': 'reconcile_state',
                                                           'description': 'For other conflicts, '
                                                                          'reconcile current state '
                                                                          'and durable replay '
                                                                          'facts; retry only an '
                                                                          'exact replay.'},
                            'job_upload_selection_cancelled': {'action': 'reconcile_state',
                                                               'description': 'For other '
                                                                              'conflicts, '
                                                                              'reconcile current '
                                                                              'state and durable '
                                                                              'replay facts; retry '
                                                                              'only an exact '
                                                                              'replay.'},
                            'job_upload_selection_pending': {'action': 'reconcile_state',
                                                             'description': 'For other conflicts, '
                                                                            'reconcile current '
                                                                            'state and durable '
                                                                            'replay facts; retry '
                                                                            'only an exact '
                                                                            'replay.'},
                            'job_upload_selection_removal_scheduled': {'action': 'reconcile_state',
                                                                       'description': 'For other '
                                                                                      'conflicts, '
                                                                                      'reconcile '
                                                                                      'current '
                                                                                      'state and '
                                                                                      'durable '
                                                                                      'replay '
                                                                                      'facts; '
                                                                                      'retry only '
                                                                                      'an exact '
                                                                                      'replay.'},
                            'job_upload_selection_retention_expired': {'action': 'reconcile_state',
                                                                       'description': 'For other '
                                                                                      'conflicts, '
                                                                                      'reconcile '
                                                                                      'current '
                                                                                      'state and '
                                                                                      'durable '
                                                                                      'replay '
                                                                                      'facts; '
                                                                                      'retry only '
                                                                                      'an exact '
                                                                                      'replay.'},
                            'operation_conflict': {'action': 'reconcile_state',
                                                   'description': 'For other conflicts, reconcile '
                                                                  'current state and durable '
                                                                  'replay facts; retry only an '
                                                                  'exact replay.'},
                            'operation_reference_conflict': {'action': 'reconcile_state',
                                                             'description': 'For other conflicts, '
                                                                            'reconcile current '
                                                                            'state and durable '
                                                                            'replay facts; retry '
                                                                            'only an exact '
                                                                            'replay.'},
                            'project_job_limit_reached': {'action': 'reconcile_state',
                                                          'description': 'For other conflicts, '
                                                                         'reconcile current state '
                                                                         'and durable replay '
                                                                         'facts; retry only an '
                                                                         'exact replay.'},
                            'project_operation_record_limit_reached': {'action': 'reconcile_state',
                                                                       'description': 'For other '
                                                                                      'conflicts, '
                                                                                      'reconcile '
                                                                                      'current '
                                                                                      'state and '
                                                                                      'durable '
                                                                                      'replay '
                                                                                      'facts; '
                                                                                      'retry only '
                                                                                      'an exact '
                                                                                      'replay.'},
                            'project_purge_in_progress': {'action': 'reconcile_state',
                                                          'description': 'For other conflicts, '
                                                                         'reconcile current state '
                                                                         'and durable replay '
                                                                         'facts; retry only an '
                                                                         'exact replay.'},
                            'project_upload_record_limit_reached': {'action': 'reconcile_state',
                                                                    'description': 'For other '
                                                                                   'conflicts, '
                                                                                   'reconcile '
                                                                                   'current state '
                                                                                   'and durable '
                                                                                   'replay facts; '
                                                                                   'retry only an '
                                                                                   'exact replay.'},
                            'project_upload_reserved_bytes_limit_exceeded': {'action': 'reconcile_state',
                                                                             'description': 'For '
                                                                                            'other '
                                                                                            'conflicts, '
                                                                                            'reconcile '
                                                                                            'current '
                                                                                            'state '
                                                                                            'and '
                                                                                            'durable '
                                                                                            'replay '
                                                                                            'facts; '
                                                                                            'retry '
                                                                                            'only '
                                                                                            'an '
                                                                                            'exact '
                                                                                            'replay.'},
                            'provider_attempt_key_conflict': {'action': 'reconcile_state',
                                                              'description': 'For other conflicts, '
                                                                             'reconcile current '
                                                                             'state and durable '
                                                                             'replay facts; retry '
                                                                             'only an exact '
                                                                             'replay.'},
                            'upload_byte_length_limit_exceeded': {'action': 'reconcile_state',
                                                                  'description': 'For other '
                                                                                 'conflicts, '
                                                                                 'reconcile '
                                                                                 'current state '
                                                                                 'and durable '
                                                                                 'replay facts; '
                                                                                 'retry only an '
                                                                                 'exact replay.'},
                            'upload_finalize_bytes_absent': {'action': 'reconcile_state',
                                                             'description': 'For other conflicts, '
                                                                            'reconcile current '
                                                                            'state and durable '
                                                                            'replay facts; retry '
                                                                            'only an exact '
                                                                            'replay.'},
                            'upload_finalize_bytes_incomplete': {'action': 'reconcile_state',
                                                                 'description': 'For other '
                                                                                'conflicts, '
                                                                                'reconcile current '
                                                                                'state and durable '
                                                                                'replay facts; '
                                                                                'retry only an '
                                                                                'exact replay.'},
                            'upload_finalize_removal_scheduled': {'action': 'reconcile_state',
                                                                  'description': 'For other '
                                                                                 'conflicts, '
                                                                                 'reconcile '
                                                                                 'current state '
                                                                                 'and durable '
                                                                                 'replay facts; '
                                                                                 'retry only an '
                                                                                 'exact replay.'},
                            'upload_linked_to_job': {'action': 'reconcile_state',
                                                     'description': 'For other conflicts, '
                                                                    'reconcile current state and '
                                                                    'durable replay facts; retry '
                                                                    'only an exact replay.'},
                            'upload_publish_already_finalized': {'action': 'reconcile_state',
                                                                 'description': 'For other '
                                                                                'conflicts, '
                                                                                'reconcile current '
                                                                                'state and durable '
                                                                                'replay facts; '
                                                                                'retry only an '
                                                                                'exact replay.'},
                            'upload_publish_removal_scheduled': {'action': 'reconcile_state',
                                                                 'description': 'For other '
                                                                                'conflicts, '
                                                                                'reconcile current '
                                                                                'state and durable '
                                                                                'replay facts; '
                                                                                'retry only an '
                                                                                'exact replay.'},
                            'upload_read_not_finalized': {'action': 'reconcile_state',
                                                          'description': 'For other conflicts, '
                                                                         'reconcile current state '
                                                                         'and durable replay '
                                                                         'facts; retry only an '
                                                                         'exact replay.'},
                            'upload_read_removal_scheduled': {'action': 'reconcile_state',
                                                              'description': 'For other conflicts, '
                                                                             'reconcile current '
                                                                             'state and durable '
                                                                             'replay facts; retry '
                                                                             'only an exact '
                                                                             'replay.'}}}
PROFILES = ({'fixed_details': {},
  'properties': {'code': {'enum': ['provider_request_invalid', 'request_content_not_supported']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 400},
                 'title': {'const': 'Bad request'},
                 'type': {'const': 'urn:nmr-api:problem:bad-request'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['authentication_failed',
                                   'authentication_nonce_reused',
                                   'authentication_window_closed',
                                   'signature_expired',
                                   'signature_created_in_future']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 401},
                 'title': {'const': 'Request authentication failed'},
                 'type': {'const': 'urn:nmr-api:problem:authentication-failed'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['authorization_denied']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 403},
                 'title': {'const': 'Authorization denied'},
                 'type': {'const': 'urn:nmr-api:problem:authorization-denied'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['request_body_timeout']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 408},
                 'title': {'const': 'Request body timeout'},
                 'type': {'const': 'urn:nmr-api:problem:request-body-timeout'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['request_path_too_large', 'request_query_too_large']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 414},
                 'title': {'const': 'URI too long'},
                 'type': {'const': 'urn:nmr-api:problem:uri-too-long'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['request_header_bytes_too_large',
                                   'request_header_count_too_large']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 431},
                 'title': {'const': 'Request header fields too large'},
                 'type': {'const': 'urn:nmr-api:problem:request-header-fields-too-large'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['internal_error',
                                   'response_validation_failed',
                                   'response_size_limit_exceeded',
                                   'http_route_memory_budget_too_small',
                                   'request_signature_processing_failed',
                                   'upload_finalize_length_mismatch',
                                   'upload_finalize_committed_facts_mismatch',
                                   'upload_finalize_storage_integrity_failed',
                                   'upload_finalize_storage_token_creation_failed',
                                   'upload_finalize_storage_response_invalid',
                                   'upload_finalize_storage_error_json_invalid',
                                   'upload_finalize_storage_error_fields_invalid',
                                   'upload_finalize_storage_error_contract_mismatch',
                                   'upload_finalize_storage_request_invalid',
                                   'job_open_inputs_inconsistent']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 500},
                 'title': {'const': 'Internal server error'},
                 'type': {'const': 'urn:nmr-api:problem:internal-error'}},
  'upload_codes': ()},
 {'fixed_details': {'http_exchange_count_exhausted': 'This server has reached its limit for '
                                                     'simultaneous requests. This attempt was '
                                                     'rejected before the operation started; it '
                                                     'made no changes. Wait briefly, then try '
                                                     'again.',
                    'http_exchange_memory_budget_exhausted': "This server's memory limit for "
                                                             'requests prevents it from accepting '
                                                             'another request. This attempt was '
                                                             'rejected before the operation '
                                                             'started; it made no changes. Wait '
                                                             'briefly, then try again.',
                    'http_exchange_runtime_not_accepting': 'This server is currently not accepting '
                                                           'new requests. This attempt was '
                                                           'rejected before the operation started; '
                                                           'it made no changes. Try again later.'},
  'properties': {'code': {'enum': ['service_unavailable',
                                   'upload_finalize_storage_authorization_rejected',
                                   'stored_analysis_result_inconsistent',
                                   'http_exchange_deadline_exceeded',
                                   'authentication_replay_ledger_unavailable',
                                   'request_signature_verifier_unavailable',
                                   'database_connection_preparation_failed',
                                   'database_executor_quarantined',
                                   'database_work_admission_stopped',
                                   'database_work_slots_exhausted',
                                   'job_create_reference_exhausted',
                                   'job_create_randomness_unavailable',
                                   'job_state_update_randomness_unavailable',
                                   'job_upload_set_randomness_unavailable',
                                   'job_provider_selection_randomness_unavailable',
                                   'description_update_randomness_unavailable',
                                   'job_mutation_clock_behind',
                                   'upload_capability_clock_behind',
                                   'upload_capability_expired_before_commit',
                                   'upload_capability_randomness_unavailable',
                                   'upload_capability_generation_failed',
                                   'job_cancellation_randomness_unavailable',
                                   'job_deletion_randomness_unavailable',
                                   'upload_finalize_randomness_unavailable',
                                   'upload_finalize_clock_behind',
                                   'upload_create_randomness_unavailable',
                                   'upload_create_reference_exhausted',
                                   'project_description_clock_behind',
                                   'upload_description_clock_behind',
                                   'upload_deletion_clock_behind',
                                   'execution_attempt_start_randomness_unavailable',
                                   'execution_attempt_start_reference_exhausted',
                                   'execution_attempt_start_clock_behind',
                                   'execution_attempt_progress_clock_behind',
                                   'execution_attempt_outcome_clock_behind',
                                   'project_list_details_unavailable',
                                   'analysis_kind_list_inactive_selection',
                                   'analysis_kind_list_missing_selection',
                                   'analysis_provider_list_selection_unavailable',
                                   'analysis_provider_list_selection_changed',
                                   'provider_directory_clock_behind',
                                   'provider_execution_attempt_capacity_exhausted',
                                   'http_route_memory_capacity_exhausted',
                                   'authentication_replay_capacity_exhausted',
                                   'upload_finalize_storage_connection_timeout',
                                   'upload_finalize_storage_request_timeout',
                                   'upload_finalize_storage_request_idle_timeout',
                                   'upload_finalize_storage_communication_failed',
                                   'upload_finalize_storage_admission_stopped',
                                   'upload_finalize_storage_slots_exhausted',
                                   'upload_finalize_storage_cleanup_unconfirmed',
                                   'upload_finalize_storage_deadline_exceeded',
                                   'upload_finalize_storage_completed_late',
                                   'database_lock_unavailable',
                                   'database_query_canceled',
                                   'database_idle_transaction_timeout',
                                   'http_exchange_count_exhausted',
                                   'http_exchange_memory_budget_exhausted',
                                   'http_exchange_runtime_not_accepting']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 503},
                 'title': {'const': 'Service unavailable'},
                 'type': {'const': 'urn:nmr-api:problem:service-unavailable'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['provider_request_invalid', 'request_query_not_supported']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 400},
                 'title': {'const': 'Bad request'},
                 'type': {'const': 'urn:nmr-api:problem:bad-request'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['resource_not_found']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 404},
                 'title': {'const': 'Resource not found'},
                 'type': {'const': 'urn:nmr-api:problem:not-found'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['operation_conflict',
                                   'execution_attempt_progress_terminal',
                                   'execution_attempt_progress_regression',
                                   'execution_attempt_completion_after_failure',
                                   'execution_attempt_failure_after_success',
                                   'execution_attempt_failure_replay_mismatch',
                                   'execution_attempt_completion_replay_mismatch',
                                   'execution_attempt_outcome_expired',
                                   'operation_reference_conflict',
                                   'provider_attempt_key_conflict',
                                   'job_provider_attempt_limit_reached',
                                   'job_attempt_limit_reached',
                                   'project_purge_in_progress',
                                   'project_job_limit_reached',
                                   'project_operation_record_limit_reached',
                                   'upload_byte_length_limit_exceeded',
                                   'project_upload_record_limit_reached',
                                   'project_upload_reserved_bytes_limit_exceeded',
                                   'deployment_upload_record_limit_reached',
                                   'deployment_upload_reserved_bytes_limit_exceeded',
                                   'job_state_change_cancelled',
                                   'job_open_pending_uploads',
                                   'upload_linked_to_job',
                                   'upload_finalize_bytes_absent',
                                   'upload_finalize_bytes_incomplete',
                                   'upload_finalize_removal_scheduled',
                                   'job_upload_selection_pending',
                                   'job_upload_selection_cancelled',
                                   'job_upload_selection_retention_expired',
                                   'job_upload_selection_removal_scheduled',
                                   'upload_publish_already_finalized',
                                   'upload_publish_removal_scheduled',
                                   'upload_read_removal_scheduled',
                                   'upload_read_not_finalized']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 409},
                 'title': {'const': 'Operation conflict'},
                 'type': {'const': 'urn:nmr-api:problem:operation-conflict'},
                 'upload_ref': {'description': 'One newly selected Upload rejected by this '
                                               'request; other inputs may also be ineligible.',
                                'pattern': '^upload:sha256:[0-9a-f]{64}(?![\\s\\S])',
                                'type': 'string'}},
  'upload_codes': ('job_upload_selection_retention_expired',
                   'job_upload_selection_removal_scheduled',
                   'job_upload_selection_pending')},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['request_content_too_large']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 413},
                 'title': {'const': 'Request content too large'},
                 'type': {'const': 'urn:nmr-api:problem:request-content-too-large'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['service_unavailable', 'http_exchange_deadline_exceeded']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 503},
                 'title': {'const': 'Change outcome unconfirmed'},
                 'type': {'const': 'urn:nmr-api:problem:mutation-outcome-unconfirmed'}},
  'upload_codes': ()},
 {'fixed_details': {},
  'properties': {'code': {'enum': ['provider_request_invalid',
                                   'request_content_not_supported',
                                   'request_query_not_supported']},
                 'detail': {'maxLength': 1024,
                            'minLength': 1,
                            'not': {'pattern': '^[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]|[\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000](?![\\s\\S])|[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff\\ufff9-\\ufffb]'},
                            'type': 'string',
                            'x-nmr-max-utf8-bytes': 1024},
                 'instance': {'maxLength': 404, 'minLength': 1, 'type': 'string'},
                 'request_id': {'maxLength': 128,
                                'minLength': 1,
                                'pattern': '^[\\u0021-\\u007e]+(?![\\s\\S])',
                                'type': 'string'},
                 'status': {'const': 400},
                 'title': {'const': 'Bad request'},
                 'type': {'const': 'urn:nmr-api:problem:bad-request'}},
  'upload_codes': ()})
