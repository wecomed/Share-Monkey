"""Produce two self-contained source-install ZIPs, without local environments."""
import argparse
from pathlib import Path
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def export(client, directory=None):
    label = 'Mac' if client == 'macos' else 'Windows'
    folder = f'Share-Monkey-{label}'
    out = (directory or ROOT / 'dist') / f'{folder}-source.zip'
    out.parent.mkdir(parents=True, exist_ok=True)
    files = [(path, str(path.relative_to(ROOT))) for path in (ROOT / 'sharemouse').rglob('*')
             if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc']
    files += [(ROOT / 'requirements.txt', 'requirements.txt'),
              (ROOT / 'requirements-build.txt', 'requirements-build.txt'),
              (ROOT / 'tools' / 'build_client.py', 'tools/build_client.py'),
              (ROOT / 'tools' / 'build_windows_installer.py', 'tools/build_windows_installer.py'),
              (ROOT / 'tools' / 'collect_licenses.py', 'tools/collect_licenses.py'),
              (ROOT / 'packaging' / 'windows.nsi', 'packaging/windows.nsi'),
              (ROOT / 'THIRD_PARTY_NOTICES.md', 'THIRD_PARTY_NOTICES.md')]
    files += [(path, f'clients/{client}/{path.name}') for path in (ROOT / 'clients' / client).glob('*.py')]
    files += [(path, path.name) for path in (ROOT / 'clients' / client).iterdir() if path.suffix != '.py' and path.is_file()]
    launcher = 'run-macos.command' if client == 'macos' else 'run-windows.bat'
    files.append((ROOT / launcher, launcher))
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            info = zipfile.ZipInfo(f'{folder}/{relative}')
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((stat.S_IFREG | (0o755 if path.suffix == '.command' else 0o644)) << 16)
            raw = path.read_bytes()
            if path.suffix == '.bat':
                raw = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            archive.writestr(info, raw)
    print(out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    for target in ('macos', 'windows'):
        export(target, args.output_dir.resolve())
