"""Build the macOS app, or a portable Windows app on Windows.

For Windows EXE installers (also buildable on macOS), use build_windows_installer.py.
"""
import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def set_bundle_version(spec, version):
    """Set supported BUNDLE options before PyInstaller assembles/signs the app."""
    tree = ast.parse(spec.read_text(encoding='utf-8'))
    bundles = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == 'BUNDLE']
    if len(bundles) != 1:
        raise RuntimeError('Mac 构建配置必须包含一个 BUNDLE')
    bundle = bundles[0]
    bundle.keywords = [item for item in bundle.keywords
                       if item.arg not in ('version', 'info_plist')]
    bundle.keywords.extend([
        ast.keyword(arg='version', value=ast.Constant(version)),
        ast.keyword(arg='info_plist', value=ast.Dict(
            keys=[ast.Constant('CFBundleShortVersionString'), ast.Constant('CFBundleVersion'),
                  ast.Constant('CFBundleDisplayName'), ast.Constant('NSHumanReadableCopyright'),
                  ast.Constant('NSLocalNetworkUsageDescription'), ast.Constant('CFBundleLocalizations'),
                  ast.Constant('NSBluetoothAlwaysUsageDescription'), ast.Constant('NSBluetoothPeripheralUsageDescription')],
            values=[ast.Constant(version), ast.Constant(version), ast.Constant('Share Monkey'),
                    ast.Constant('Share Monkey contributors'),
                    ast.Constant('在同一局域网中发现并连接你的电脑，共享键鼠、剪贴板与文件。'),
                    ast.List(elts=[ast.Constant('zh-Hans'), ast.Constant('en')], ctx=ast.Load()),
                    ast.Constant('通过蓝牙连接你的另一台电脑，共享键鼠和文本剪贴板，无需 Wi-Fi。'),
                    ast.Constant('通过蓝牙连接你的另一台电脑，共享键鼠和文本剪贴板。')])),
    ])
    ast.fix_missing_locations(tree)
    spec.write_text(ast.unparse(tree) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('client', choices=('macos', 'windows'))
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    expected = 'darwin' if args.client == 'macos' else 'win32'
    if sys.platform != expected:
        parser.error(f'{args.client} 客户端必须在对应系统构建')
    sys.path.insert(0, str(ROOT))
    from sharemouse import __version__
    from sharemouse.runtime import configure_tk
    from tools.collect_licenses import collect
    configure_tk()
    notices = collect(['pynput', 'cryptography', 'cffi', 'pycparser', 'six', 'pyperclip',
                       'pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-Quartz',
                       'pyobjc-framework-CoreBluetooth', 'PyInstaller']
                      if args.client == 'macos' else
                      ['pynput', 'cryptography', 'cffi', 'pycparser', 'six', 'pyperclip', 'PyInstaller'],
                      ROOT / 'build' / 'licenses' / args.client, python=True)
    name = 'Share Monkey' if args.client == 'macos' else 'Share Monkey Windows'
    backend = '_darwin' if args.client == 'macos' else '_win32'
    command = [sys.executable, '-m', 'PyInstaller.utils.cliutils.makespec',
               '--windowed', '--onedir', '--name', name, '--paths', str(ROOT),
               '--specpath', str(ROOT / 'build'),
               '--hidden-import', f'pynput.keyboard.{backend}',
               '--hidden-import', f'pynput.mouse.{backend}',
               '--hidden-import', f'pynput._util.{backend[1:]}',
               '--add-data', str(ROOT / 'sharemouse' / 'assets') + os.pathsep + 'sharemouse/assets',
               '--add-data', str(ROOT / 'THIRD_PARTY_NOTICES.md') + os.pathsep + '.',
               '--add-data', str(notices) + os.pathsep + 'licenses',
               '--icon', str(ROOT / 'sharemouse' / 'assets' / ('monkey.icns' if args.client == 'macos' else 'monkey.ico')),
               '--exclude-module', 'sharemouse.legacy_tk', '--exclude-module', 'sharemouse.ui',
               '--exclude-module', 'tkinter']
    if args.client == 'macos':
        # Preserve the original ID so existing input/privacy permissions can migrate.
        command.extend(['--osx-bundle-identifier', 'local.sharemouselab.mac',
                        '--hidden-import', 'CoreBluetooth',
                        '--exclude-module', 'sharemouse.native_windows', '--exclude-module', 'PySide6',
                        '--exclude-module', 'sharemouse.bluetooth_windows', '--exclude-module', 'bleak'])
    else:
        command.extend(['--exclude-module', 'sharemouse.native_mac', '--exclude-module', 'sharemouse.bluetooth_mac',
                        '--hidden-import', 'bleak.backends.winrt.client',
                        '--hidden-import', 'bleak.backends.winrt.scanner'])
    command.append(str(ROOT / 'clients' / args.client / 'entry.py'))
    env = dict(os.environ)
    env['PYINSTALLER_CONFIG_DIR'] = str(ROOT / 'build' / 'pyinstaller-cache')
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    spec = ROOT / 'build' / f'{name}.spec'
    if args.client == 'macos':
        set_bundle_version(spec, __version__)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(args.output_dir.resolve()),
                    '--workpath', str(ROOT / 'build' / args.client), str(spec)],
                   cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
