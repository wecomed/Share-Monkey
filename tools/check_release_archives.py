"""Check release ZIP integrity and source bytes; does not execute either client."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sharemouse import __version__


def check(directory, report, windows_only=False):
    results = {}
    bundle = directory / 'Share Monkey.app'
    mac_zip = directory / f'Share-Monkey-{__version__}-Mac.zip'
    if not windows_only:
        with zipfile.ZipFile(mac_zip) as archive:
            assert archive.testzip() is None, 'Mac ZIP CRC failure'
            checked = 0
            for path in bundle.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    name = 'Share Monkey.app/' + path.relative_to(bundle).as_posix()
                    assert archive.read(name) == path.read_bytes(), name
                    checked += 1
            results[mac_zip.name] = {'crc': True, 'bundle_files_verified': checked}
    for label in ('Mac', 'Windows'):
        path = directory / f'Share-Monkey-{label}-source.zip'
        folder = f'Share-Monkey-{label}/'
        client = 'macos' if label == 'Mac' else 'windows'
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None, f'{label} source ZIP CRC failure'
            count = 0
            for name in archive.namelist():
                if name.endswith('.py'):
                    compile(archive.read(name), name, 'exec')
                    count += 1
            for source in (ROOT / 'sharemouse').rglob('*'):
                if (source.is_file() and '__pycache__' not in source.parts
                        and source.suffix != '.pyc'):
                    assert archive.read(folder + source.relative_to(ROOT).as_posix()) == source.read_bytes()
            assert archive.read(folder + 'README.md') == (ROOT / 'clients' / client / 'README.md').read_bytes()
            results[path.name] = {'crc': True, 'python_files_compiled': count,
                                  'current_module_asset_and_help_bytes': True}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    releases = ([] if windows_only else [mac_zip]) + [directory / f'Share-Monkey-{__version__}-Windows-Setup.exe',
                directory / 'Share-Monkey-Mac-source.zip', directory / 'Share-Monkey-Windows-source.zip']
    hashes = [f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}' for path in releases]
    (directory / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='ascii')
    print('PASS: all release ZIP CRCs, bundle/source bytes, Python compilation, SHA-256 manifest')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'dist' / f'v{__version__}')
    parser.add_argument('--report', type=Path, default=ROOT / 'validation' / 'v053' / 'archives.json')
    parser.add_argument('--windows-only', action='store_true',
                        help='Windows maintenance release; preserve the compatible existing Mac app')
    args = parser.parse_args()
    check(args.directory.resolve(), args.report.resolve(), windows_only=args.windows_only)
