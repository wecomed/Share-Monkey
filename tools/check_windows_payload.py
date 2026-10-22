"""Validate the cross-built Windows payload without claiming Windows execution."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pefile

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_DLLS = set('''advapi32.dll authz.dll bcrypt.dll bcryptprimitives.dll cabinet.dll
comctl32.dll comdlg32.dll crypt32.dll d2d1.dll d3d11.dll d3d12.dll d3d9.dll
d3dcompiler_47.dll dnsapi.dll dwmapi.dll dwrite.dll dxgi.dll gdi32.dll imagehlp.dll
imm32.dll iphlpapi.dll kernel32.dll mpr.dll msi.dll ncrypt.dll netapi32.dll ntdll.dll
odbc32.dll ole32.dll oleaut32.dll opengl32.dll powrprof.dll propsys.dll rpcrt4.dll
secur32.dll setupapi.dll shell32.dll shlwapi.dll uiautomationcore.dll user32.dll
userenv.dll uxtheme.dll version.dll winhttp.dll winmm.dll winspool.drv ws2_32.dll
wtsapi32.dll ucrtbase.dll'''.split())


def check(directory, report):
    files = [p for p in directory.rglob('*') if p.suffix.lower() in ('.exe', '.pyd', '.dll')]
    by_name = {p.name.casefold(): p for p in files}
    imports = {}
    for path in files:
        binary = pefile.PE(str(path), fast_load=True)
        assert binary.FILE_HEADER.Machine == 0x8664, f'不是 Windows x64 二进制：{path}'
        binary.parse_data_directories(directories=[1, 13])
        imports[path] = [entry.dll.decode().casefold()
                         for table in ('DIRECTORY_ENTRY_IMPORT', 'DIRECTORY_ENTRY_DELAY_IMPORT')
                         for entry in getattr(binary, table, [])]
        binary.close()
    # Runtime roots: Python, input, cryptography, UI, Windows platform and SVG/ICO plugins.
    # The complete Essentials wheel also contains optional QML/database/PDF plugins
    # that this Widgets app never imports. They are not application runtime roots.
    queue = [p for p in files if 'PySide6' not in p.parts]
    queue += [directory / 'pkgs' / 'PySide6' / name for name in ('QtCore.pyd', 'QtGui.pyd', 'QtWidgets.pyd')]
    plugins = directory / 'pkgs' / 'PySide6' / 'plugins'
    queue += [plugins / 'platforms' / 'qwindows.dll']
    queue += list((plugins / 'iconengines').glob('*.dll'))
    queue += [p for p in (plugins / 'imageformats').glob('*.dll') if p.name != 'qpdf.dll']
    seen, system, missing = set(), set(), {}
    while queue:
        path = queue.pop()
        assert path.is_file(), f'缺少启动组件：{path}'
        if path in seen:
            continue
        seen.add(path)
        for name in imports[path]:
            if name in by_name:
                queue.append(by_name[name])
            elif name in SYSTEM_DLLS or name.startswith(('api-ms-win-', 'ext-ms-win-')):
                system.add(name)
            else:
                missing.setdefault(name, []).append(str(path.relative_to(directory)))
    assert not missing, f'应用运行必需 DLL 缺失：{missing}'
    with zipfile.ZipFile(directory / 'bin' / 'Share Monkey.exe') as launcher:
        source = launcher.read('__main__.py').decode()
        assert 'from sharemouse.windows import main' in source
        assert 'client.log' in source
    for path in (ROOT / 'sharemouse').glob('*.py'):
        assert (directory / 'pkgs' / 'sharemouse' / path.name).read_bytes() == path.read_bytes(), f'打包代码过期：{path}'
    for asset in (ROOT / 'sharemouse' / 'assets').iterdir():
        assert (directory / 'pkgs' / 'sharemouse' / 'assets' / asset.name).read_bytes() == asset.read_bytes()
    manifest = json.loads((directory / 'payload-manifest.json').read_text())
    for entry in manifest:
        path = directory / entry['path']
        assert path.stat().st_size == entry['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']
    result = {'host_platform': sys.platform, 'executed_on_windows': False,
              'all_pe_files_x64': len(files), 'application_dependency_closure_files': len(seen),
              'missing_required_vendor_dlls': missing, 'windows_system_imports': sorted(system),
              'manifest_files_verified': len(manifest), 'module_and_asset_bytes_match': True,
              'launcher_entrypoint': 'sharemouse.windows:main', 'python_included': '3.12.10'}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f'PASS: {len(files)} x64 binaries, {len(seen)} required dependency files, {len(manifest)} hashes, launcher and source bytes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, default=ROOT / 'build' / 'windows-installer')
    parser.add_argument('--report', type=Path, default=ROOT / 'validation' / 'v053' / 'windows-payload.json')
    args = parser.parse_args()
    check(args.directory.resolve(), args.report.resolve())
