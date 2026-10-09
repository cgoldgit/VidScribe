"""Structured writing transport; deterministic metadata never needs model output."""
import copy
import json
import re


_DIAGNOSTIC_CODES = frozenset({
    'type', 'required', 'unexpected', 'min_items', 'max_items', 'min_length',
    'enum', 'pattern', 'missing_output', 'response_type', 'invalid_result',
    'unsupported_schema',
})
_DIAGNOSTIC_KEYS = frozenset({
    'structured_output', 'draft_md', 'review', 'coverage', 'source_ranges',
    'status', 'section_id', 'reason', 'visual_requests', 'time',
    'visual_review', 'visual_review_reason', 'fact_review', 'fact_review_reason',
    'corrections', 'kind', 'original', 'replacement', 'display', 'evidence',
    'detail', 'url', 'uncertainties', 'boundary_notes',
})


def _safe_diagnostic_path(path):
    """Allow schema-owned names and indexes; never echo an external object key."""
    if (not isinstance(path, str) or len(path) > 512
            or re.fullmatch(r'\$(?:\.[a-z_]+|\[[0-9]+\])*', path) is None
            or any(key not in _DIAGNOSTIC_KEYS for key in re.findall(r'\.([a-z_]+)', path))):
        return '$'
    return path


class ResultValidationError(ValueError):
    """A safe diagnostic that exposes location/category, never response values."""

    def __init__(self, code, path='$'):
        self.code = code if isinstance(code, str) and code in _DIAGNOSTIC_CODES else 'invalid_result'
        self.path = _safe_diagnostic_path(path)
        super().__init__('Invalid structured output')


def _object(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}


def writing_schema():
    text = {'type': 'string'}
    nonempty = {'type': 'string', 'minLength': 1}
    identifier = {'type': 'string', 'pattern': r'^S[0-9]{6,}$'}
    ranges = {'type': 'array', 'minItems': 1, 'items':
              {'type': 'array', 'minItems': 2, 'maxItems': 2, 'items': identifier}}
    evidence = _object({'kind': {'type': 'string', 'enum': ['reference', 'calculation', 'execution']},
                        'detail': nonempty, 'url': text}, ['kind', 'detail'])
    coverage = _object({'source_ranges': ranges, 'status': {'type': 'string', 'enum': ['written', 'omitted']},
                        'section_id': text, 'reason': text})
    visual = _object({'time': {'type': 'string', 'pattern': r'^[0-9]{2}:[0-9]{2}:[0-9]{2}$'},
                      'status': {'type': 'string', 'enum': ['pending', 'omitted']},
                      'reason': nonempty, 'section_id': nonempty})
    correction = _object({'kind': {'type': 'string', 'enum': ['lecture_error', 'transcription']},
                          'status': {'type': 'string', 'enum': ['verified', 'unresolved']},
                          'source_ranges': ranges, 'section_id': nonempty, 'original': nonempty,
                          'replacement': text, 'display': nonempty,
                          'evidence': {'type': 'array', 'items': evidence}})
    review = _object({'coverage': {'type': 'array', 'minItems': 1, 'items': coverage},
                      'visual_requests': {'type': 'array', 'items': visual},
                      'visual_review': {'type': 'string', 'enum': ['pending']},
                      'visual_review_reason': nonempty,
                      'fact_review': {'type': 'string', 'enum': ['pending', 'complete']},
                      'fact_review_reason': nonempty,
                      'corrections': {'type': 'array', 'items': correction},
                      'uncertainties': {'type': 'array', 'items': nonempty},
                      'boundary_notes': {'type': 'array', 'items': nonempty}})
    return _object({'draft_md': nonempty, 'review': review})


def _shape(value, schema, path='$'):
    """Validate the small schema subset used above, without a new runtime dependency."""
    if not isinstance(schema, dict):
        raise ResultValidationError('unsupported_schema', path)
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict):
            raise ResultValidationError('type', path)
        for key in schema['required']:
            if key not in value:
                raise ResultValidationError('required', path + '.' + key)
        if set(value) - schema['properties'].keys():
            raise ResultValidationError('unexpected', path)
        for key, item in value.items():
            _shape(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list):
            raise ResultValidationError('type', path)
        if len(value) < schema.get('minItems', 0):
            raise ResultValidationError('min_items', path)
        if len(value) > schema.get('maxItems', len(value)):
            raise ResultValidationError('max_items', path)
        for index, item in enumerate(value):
            _shape(item, schema['items'], f'{path}[{index}]')
    elif kind == 'string':
        if not isinstance(value, str):
            raise ResultValidationError('type', path)
        if len(value) < schema.get('minLength', 0):
            raise ResultValidationError('min_length', path)
        if 'enum' in schema and value not in schema['enum']:
            raise ResultValidationError('enum', path)
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            raise ResultValidationError('pattern', path)
    else:
        raise ResultValidationError('unsupported_schema', path)


def stamp_review(review, metadata):
    """Fill only known provenance constants. Conflicting model values are rejected."""
    result = copy.deepcopy(review)
    for key in ('source_sha256', 'packet_id', 'editorial_policy_version'):
        expected = metadata.get(key)
        if expected is None:
            continue
        if key in result and result[key] != expected:
            raise ValueError('Conflicting review provenance')
        result[key] = expected
    for entry in result.get('coverage', []):
        if 'packet_id' in entry and entry['packet_id'] != metadata['packet_id']:
            raise ValueError('Conflicting coverage packet')
        entry['packet_id'] = metadata['packet_id']
    return result


def decode(response, metadata):
    if not isinstance(response, dict):
        raise ResultValidationError('response_type')
    if 'structured_output' not in response:
        raise ResultValidationError('missing_output', '$.structured_output')
    payload = response.get('structured_output')
    _shape(payload, writing_schema(), '$.structured_output')
    return payload['draft_md'], stamp_review(payload['review'], metadata)


def transport_prompt(prompt):
    return (prompt + '\n\n# 本次输出协议（替代上述文件写入与回传路径要求）\n'
            '本次所有原句与规则已完整提供，没有文件工具。直接返回 schema 指定的 draft_md 与 review；'
            'draft_md 放完整 Markdown，review 放实际审阅内容，由程序写入文件。不要执行 Read/Write/Edit，'
            '不要只返回路径，也不要把正文缩成提纲。source_sha256、packet_id、editorial_policy_version '
            '由程序补齐，不要输出；小节锚点与 source_ranges 仍使用本包实际 ID。'
            'uncertainties 和 boundary_notes 只填真实疑点或未完内容，没有就用 []。'
            '本次没有图片工具，visual_review 保持 pending，不声称完成核图。'
            'corrections.display 写对应正文的完整勘误/待核说明，不只写标记；evidence 无依据时用 []。\n')


def schema_argument():
    return json.dumps(writing_schema(), ensure_ascii=False, separators=(',', ':'))
