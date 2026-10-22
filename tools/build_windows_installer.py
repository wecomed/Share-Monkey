"""Create a Windows x64 EXE installer on Windows, macOS, or Linux.

Uses the official embeddable CPython runtime and Windows wheels, never macOS
extension modules. NSIS only compiles the installer; it does not run Windows code.
"""
import argparse
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NSIS_VERSION = '3.11'
NSIS_HASHES = {
    'nsis-3.11-src.tar.bz2': '19e72062676ebdc67c11dc032ba80b979cdbffd3886c60b04bb442cdd401ff4b',
    'nsis-3.11.zip': 'c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1',
}
WINDOWS_WHEELS = [
    'PySide6-Essentials==6.8.3', 'shiboken6==6.8.3', 'pynput==1.8.1',
    'cryptography==46.0.7', 'cffi==2.0.0', 'pycparser==2.23', 'six==1.17.0',
    'bleak==3.0.2', 'typing_extensions==4.15.0', 'winrt-runtime==3.2.1',
    'winrt-windows-devices-bluetooth==3.2.1',
    'winrt-windows-devices-bluetooth-advertisement==3.2.1',
    'winrt-windows-devices-bluetooth-genericattributeprofile==3.2.1',
    'winrt-windows-devices-enumeration==3.2.1', 'winrt-windows-devices-radios==3.2.1',
    'winrt-windows-foundation==3.2.1', 'winrt-windows-foundation-collections==3.2.1',
    'winrt-windows-storage-streams==3.2.1',
]


def fetch_windows_wheels():
    """Verify complete downloads before Pynsist sees them; interrupted files are never cached."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from packaging.utils import parse_wheel_filename
    from nsist.wheels import compatible_tags
    tags = list(compatible_tags((3, 12), 'win_amd64'))
    cache = ROOT / 'build' / 'windows-wheels'
    cache.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=4, connect=4, read=4, status=4, other=4, backoff_factor=.5,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=('GET',))))
    paths = []
    for requirement in WINDOWS_WHEELS:
        name, version = requirement.split('==')
        response = session.get(f'https://pypi.org/pypi/{name}/{version}/json', timeout=(15, 40))
        response.raise_for_status()
        candidates = []
        for file in response.json()['urls']:
            if not file['filename'].endswith('.whl'):
                continue
            wheel_tags = {(t.interpreter, t.abi, t.platform) for t in parse_wheel_filename(file['filename'])[3]}
            matching = [i for i, tag in enumerate(tags) if tag in wheel_tags]
            if matching:
                candidates.append((min(matching), file))
        if not candidates:
            raise RuntimeError('没有兼容的 Windows wheel：' + requirement)
        file = min(candidates, key=lambda c: c[0])[1]
        path = cache / file['filename']
        expected = file['digests']['sha256']
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            temporary = path.with_suffix('.partial')
            for attempt in range(4):
                try:
                    print('Downloading', file['filename'], 'attempt', attempt + 1, flush=True)
                    with session.get(file['url'], stream=True, timeout=(30, 90)) as download:
                        download.raise_for_status()
                        with temporary.open('wb') as target:
                            for chunk in download.iter_content(1024 * 1024):
                                target.write(chunk)
                    if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
                        raise RuntimeError('Windows wheel 校验失败：' + str(path))
                    temporary.replace(path)
                    break
                except (requests.RequestException, RuntimeError):
                    temporary.unlink(missing_ok=True)
                    if attempt == 3:
                        raise
        paths.append(str(path))
    return paths


def makensis():
    found = shutil.which('makensis')
    if found:
        return found
    if sys.platform == 'win32':
        from nsist import find_makensis_win
        return find_makensis_win()
    import requests
    base = ROOT / 'build' / 'toolchains'
    prefix = base / f'nsis-{NSIS_VERSION}'
    executable = prefix / 'makensis'
    if not executable.exists() or not (prefix / '.share-monkey-version').exists():
        base.mkdir(parents=True, exist_ok=True)
        for filename, expected in NSIS_HASHES.items():
            path = base / filename
            if not path.exists():
                response = requests.get(f'https://downloads.sourceforge.net/project/nsis/NSIS%203/{NSIS_VERSION}/{filename}', timeout=120)
                response.raise_for_status()
                path.write_bytes(response.content)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError('NSIS 下载校验失败：' + filename)
            if filename.endswith('.zip'):
                with zipfile.ZipFile(path) as archive:
                    archive.extractall(base)
            else:
                with tarfile.open(path) as archive:
                    archive.extractall(base, filter='data')
        subprocess.run([sys.executable, '-m', 'SCons', '-j4', f'VERSION={NSIS_VERSION}',
                        'SKIPSTUBS=all', 'SKIPPLUGINS=all', 'SKIPUTILS=all', 'SKIPMISC=all',
                        'NSIS_CONFIG_CONST_DATA_PATH=no', f'PREFIX={prefix}', 'install-compiler'],
                       cwd=base / f'nsis-{NSIS_VERSION}-src', check=True)
        (prefix / '.share-monkey-version').write_text(NSIS_VERSION)
    os.environ['NSISDIR'] = str(prefix)
    return str(executable)


def build(output_dir, payload_dir=None):
    import nsist
    sys.path.insert(0, str(ROOT))
    from sharemouse import __version__
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    os.environ.setdefault('PYNSIST_CACHE_DIR', str(ROOT / 'build' / 'download-cache'))
    compiler = makensis()
    wheels = fetch_windows_wheels()
    directory = payload_dir or ROOT / 'build' / 'windows-installer'
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    builder = nsist.InstallerBuilder('Share Monkey', __version__, {},
                                    py_version='3.12.10', py_bitness=64, inc_msvcrt=False,
                                    build_dir=str(directory), packages=['sharemouse', 'pyperclip'],
                                    local_wheels=wheels)
    builder.fetch_python_embeddable()
    builder.prepare_packages()
    # A GUI PE launcher uses the bundled interpreter relative to its own folder.
    # Unlike a batch file, it never flashes a console and also works from shortcuts.
    preamble = '''
from pathlib import Path
logdir = Path(os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))) / 'Share Monkey' / 'logs'
logdir.mkdir(parents=True, exist_ok=True)
if 'pythonw' in sys.executable.lower():
    sys.stdout = sys.stderr = (logdir / 'client.log').open('w', encoding='utf-8', errors='replace')
'''
    commands = {'Share Monkey': {'entry_point': 'sharemouse.windows:main', 'console': False,
                                 'extra_preamble': io.StringIO(preamble)}}
    from nsist.commands import prepare_bin_directory
    (directory / 'bin').mkdir()
    prepare_bin_directory(directory / 'bin', commands, bitness=64)
    shutil.copy2(ROOT / 'sharemouse' / 'assets' / 'monkey.ico', directory / 'monkey.ico')
    shutil.copy2(ROOT / 'THIRD_PARTY_NOTICES.md', directory / 'THIRD_PARTY_NOTICES.md')
    from tools.collect_licenses import collect
    collect(['pyperclip'], directory / 'licenses')
    # Keep Qt and Python license information, including wheel metadata, installed.
    manifest = []
    for path in sorted(directory.rglob('*')):
        if path.is_file():
            manifest.append({'path': str(path.relative_to(directory)), 'bytes': path.stat().st_size,
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    (directory / 'payload-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    output_dir.mkdir(parents=True, exist_ok=True)
    installer = output_dir / f'Share-Monkey-{__version__}-Windows-Setup.exe'
    subprocess.run([compiler, f'-DVERSION={__version__}', f'-DPAYLOAD={directory}',
                    f'-DOUTFILE={installer}', str(ROOT / 'packaging' / 'windows.nsi')], check=True)
    # Keep the manifest beside the release for repeatable package validation.
    shutil.copy2(directory / 'payload-manifest.json', output_dir / 'windows-payload-manifest.json')
    print(installer)
    return installer


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist' / 'windows-dev')
    parser.add_argument('--payload-dir', type=Path)
    args = parser.parse_args()
    build(args.output_dir.resolve(), args.payload_dir.resolve() if args.payload_dir else None)
