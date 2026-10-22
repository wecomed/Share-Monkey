"""Small, dependency-free evidence contract shared by validation tools.

A source fingerprint identifies what the harness loaded.  It is deliberately
separate from tested_version/runtime: source tests cannot attest to an installed
binary just because they borrowed that binary's dependencies.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys

SCHEMA_VERSION = 1
STATUSES = ('passed', 'failed', 'not_run', 'interrupted')
ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_utc(value):
    if not isinstance(value, str):
        raise ValueError('A UTC timestamp is required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return result.astimezone(timezone.utc)


def source_context(root=None, *, tested_version=None, runtime=None):
    root = Path(root or ROOT).resolve()
    paths = sorted((root / 'sharemouse').rglob('*.py'))
    # Include all portable/probe harness source, never outputs, caches or logs.
    paths += sorted((root / 'tools').glob('*.py'))
    paths += sorted((root / 'tests').glob('test_*.py'))
    files = []
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        name = path.relative_to(root).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(dict(path=name, sha256=file_hash))
        digest.update((name + '\0' + file_hash + '\n').encode('utf-8'))
    version_source = (root / 'sharemouse' / '__init__.py').read_text(encoding='utf-8')
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", version_source, re.M)
    if match is None or not files:
        raise ValueError('Cannot identify the source version and fingerprint')
    version = match.group(1)
    return dict(source_fingerprint=dict(algorithm='sha256', digest=digest.hexdigest(), files=files),
                harness_version=version, tested_version=tested_version or version,
                platform=sys.platform, platform_release=platform.release(),
                python=sys.version.split()[0],
                runtime=runtime or dict(kind='source', path=str(root)))


def make_report(check_id, status, *, evidence=None, context=None,
                evidence_scope='model', started_at=None, reason=None):
    if status not in STATUSES:
        raise ValueError('Unknown final validation status: ' + str(status))
    return dict(schema_version=SCHEMA_VERSION, check_id=str(check_id), status=status,
                started_at=started_at or utc_now(), completed_at=utc_now(),
                context=context if context is not None else source_context(),
                evidence_scope=evidence_scope, evidence=evidence if evidence is not None else {},
                reason=reason)


def write_report(report, output_path):
    """Write atomically; create a concise human-readable sibling report too."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    context = report.get('context', {})
    text = [f"# {report.get('check_id', 'Validation')}", '',
            f"Status: **{report.get('status', 'not_run')}**", '',
            f"Scope: {report.get('evidence_scope', 'unspecified')}",
            f"UTC: {report.get('completed_at', 'unknown')}",
            f"Tested version: {context.get('tested_version', 'unknown')}; "
            f"harness: {context.get('harness_version', 'unknown')}",
            f"Platform: {context.get('platform', 'unknown')}",
            f"Source SHA-256: {context.get('source_fingerprint', {}).get('digest', 'unknown')}"]
    if report.get('reason'):
        text.extend(('', str(report['reason'])))
    checks = report.get('evidence', {}).get('checks', [])
    if checks:
        text.extend(('', '| Check | Status | Reason | Log |', '|---|---|---|---|'))
        for check in checks:
            cells = [check.get('check_id', ''), check.get('status', ''),
                     check.get('reason') or '', check.get('log', '')]
            text.append('| ' + ' | '.join(str(c).replace('|', '\\|').replace('\n', ' ') for c in cells) + ' |')
    text.extend(('', 'The JSON file contains the complete evidence and limitations.', ''))
    path.with_suffix('.md').write_text('\n'.join(text), encoding='utf-8')
    return path


def evidence_problem(value, path='evidence'):
    """Return (status, reason) for failure markers, including nested probes.

    A zero process exit code is insufficient.  A skipped native check or an
    empty declared phase list is incomplete, not a successful run.
    """
    problems = []
    if isinstance(value, dict):
        if value.get('passed') is False:
            problems.append(('failed', path + '.passed is false'))
        for name in ('errors', 'failures', 'workers_alive', 'listeners_alive', 'unexpected_successes'):
            if value.get(name):
                problems.append(('failed', path + '.' + name + ' is nonempty'))
        if value.get('driver_alive'):
            problems.append(('failed', path + '.driver_alive is true'))
        if value.get('skipped') or value.get('expected_failures') or value.get('status') in ('not_run', 'skipped'):
            problems.append(('not_run', path + ' was skipped or not run'))
        if value.get('status') == 'interrupted':
            problems.append(('interrupted', path + ' was interrupted'))
        if value.get('status') == 'failed':
            problems.append(('failed', path + ' reports failure'))
        if 'phases' in value and not value['phases']:
            problems.append(('not_run', path + '.phases is empty'))
        for name, item in value.items():
            problem = evidence_problem(item, path + '.' + str(name))
            if problem:
                problems.append(problem)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problem = evidence_problem(item, path + '[' + str(index) + ']')
            if problem:
                problems.append(problem)
    priority = {'interrupted': 3, 'failed': 2, 'not_run': 1}
    return max(problems, key=lambda item: priority[item[0]]) if problems else None
