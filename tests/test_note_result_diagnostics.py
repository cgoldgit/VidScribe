"""Precise structured-result diagnostics cannot echo response secrets."""
import copy
from pathlib import Path
import sys

SRC = Path(__file__).resolve().parent.parent / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import note_results


SECRET = 'private-provider-token-never-log-this-value'
METADATA = {'packet_id': 'P001', 'source_sha256': 'source-hash', 'editorial_policy_version': 1}


def _payload():
    return {'draft_md': '## 课堂例子\n\n完整课堂正文。', 'review': {
        'coverage': [{'source_ranges': [['S000001', 'S000002']], 'status': 'written',
                      'section_id': 'P001.example', 'reason': ''}],
        'visual_requests': [], 'visual_review': 'pending', 'visual_review_reason': '需要独立核图。',
        'fact_review': 'complete', 'fact_review_reason': '已按原句核查事实。', 'corrections': [],
        'uncertainties': [], 'boundary_notes': [],
    }}


def _error(action, code, path):
    try:
        action()
    except note_results.ResultValidationError as error:
        assert isinstance(error, ValueError)
        assert error.code == code
        assert error.path == path
        assert str(error) == 'Invalid structured output'
        assert SECRET not in str(error)
        assert SECRET not in repr(error)
        assert SECRET not in repr(vars(error))
        assert SECRET not in repr(error.args)
        return error
    raise AssertionError('Invalid structured output was accepted')


def test_decode_reports_response_and_missing_result_safely():
    for response in (None, [SECRET], SECRET, 1):
        _error(lambda: note_results.decode(response, METADATA), 'response_type', '$')
    _error(lambda: note_results.decode({SECRET: SECRET}, METADATA),
           'missing_output', '$.structured_output')
    _error(lambda: note_results.decode({'structured_output': None}, METADATA),
           'type', '$.structured_output')


def test_missing_field_path_is_stable_and_owned_by_schema():
    payload = _payload()
    del payload['review']['fact_review_reason']
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'required', '$.structured_output.review.fact_review_reason')


def test_extra_secret_field_name_is_not_reported():
    payload = _payload()
    payload['review'][SECRET] = {SECRET: SECRET}
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'unexpected', '$.structured_output.review')
    payload = _payload()
    payload['review']['coverage'][0][SECRET] = SECRET
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'unexpected', '$.structured_output.review.coverage[0]')


def test_nested_type_and_source_id_errors_have_exact_safe_locations():
    payload = _payload()
    payload['review']['coverage'][0]['source_ranges'] = SECRET
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'type', '$.structured_output.review.coverage[0].source_ranges')
    payload = _payload()
    payload['review']['coverage'][0]['source_ranges'][0][1] = SECRET
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'pattern', '$.structured_output.review.coverage[0].source_ranges[0][1]')


def test_array_length_diagnostics_distinguish_minimum_and_maximum():
    payload = _payload()
    payload['review']['coverage'] = []
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'min_items', '$.structured_output.review.coverage')
    payload = _payload()
    payload['review']['coverage'][0]['source_ranges'][0].append(SECRET)
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'max_items', '$.structured_output.review.coverage[0].source_ranges[0]')


def test_string_length_enum_and_type_diagnostics_do_not_echo_values():
    payload = _payload()
    payload['draft_md'] = ''
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'min_length', '$.structured_output.draft_md')
    payload = _payload()
    payload['review']['visual_review'] = SECRET
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'enum', '$.structured_output.review.visual_review')
    payload = _payload()
    payload['draft_md'] = {SECRET: SECRET}
    _error(lambda: note_results.decode({'structured_output': payload}, METADATA),
           'type', '$.structured_output.draft_md')


def test_error_constructor_restricts_public_codes_and_paths():
    error = note_results.ResultValidationError(SECRET, '$.' + SECRET)
    assert error.code == 'invalid_result'
    assert error.path == '$'
    assert SECRET not in repr(vars(error))
    for path in ('$.review.' + SECRET, '$.review["' + SECRET + '"]',
                 '$.review\n' + SECRET, '$.unknown_field', '$.' + 'review.' * 1000):
        error = note_results.ResultValidationError('type', path)
        assert error.path == '$'
        assert SECRET not in repr(vars(error))
    error = note_results.ResultValidationError([SECRET], [SECRET])
    assert error.code == 'invalid_result' and error.path == '$'


def test_missing_untrusted_schema_key_cannot_leak_through_path():
    schema = {'type': 'object', 'required': [SECRET], 'properties': {SECRET: {'type': 'string'}}}
    _error(lambda: note_results._shape({}, schema), 'required', '$')
    _error(lambda: note_results._shape(SECRET, {'type': SECRET}), 'unsupported_schema', '$')


def test_valid_results_keep_stamping_and_input_immutability():
    payload = _payload()
    original = copy.deepcopy(payload)
    draft, review = note_results.decode({'structured_output': payload}, METADATA)
    assert draft == payload['draft_md']
    assert all(review[key] == value for key, value in METADATA.items())
    assert review['coverage'][0]['packet_id'] == 'P001'
    assert payload == original
    assert 'packet_id' not in payload['review']
