"""Build an offline Windows validation-source ZIP; never rebuild the product.

The target already-installed Share Monkey supplies Python and third-party
dependencies. The harness is from this bundle; the one-click probe explicitly
loads the input adapters from installed pkgs and verifies their provenance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]

BOOTSTRAP = r'''"""Offline bootstrap for the existing Share Monkey embedded Python."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import zipfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--native', action='store_true')
parser.add_argument('--installed-root', type=Path)
args = parser.parse_args()
runtime = args.installed_root or Path(sys.executable).resolve().parent.parent
pkgs = runtime / 'pkgs'
if not pkgs.is_dir():
    raise SystemExit('Cannot find installed Share Monkey pkgs: ' + str(pkgs))
sys.path.insert(0, str(pkgs))
sys.path.insert(0, str(root))
os.chdir(root)
reports = root / 'reports' / time.strftime('%Y%m%d-%H%M%S')
reports.mkdir(parents=True, exist_ok=True)
execution_errors = []

def invoke(function, argv):
    try:
        return int(function(argv) or 0)
    except SystemExit as exc:
        if isinstance(exc.code, (int, type(None))):
            return int(exc.code or 0)
        message = str(exc.code)
        execution_errors.append(dict(tool=function.__module__, error='SystemExit', message=message))
        print('SystemExit: ' + message)
        return 1
    except Exception as exc:
        message = str(exc)
        execution_errors.append(dict(tool=function.__module__, error=type(exc).__name__, message=message))
        print(type(exc).__name__ + ': ' + message)
        return 1

from tools.collect_paired_evidence import main as observe
from tools.probe_windows_native import main as probe
observation = invoke(observe, ['observe', '--duration', '15', '--output',
                              str(reports / 'windows-peer.json'),
                              '--installed-root', str(runtime)])
native_args = ['--output', str(reports / 'windows-native.json'), '--target', 'installed',
               '--installed-root', str(runtime)]
if args.native:
    native_args.append('--native')
native = invoke(probe, native_args)
results_archive = reports.parent / ('Share-Monkey-Windows-Results-' + reports.name + '.zip')
summary = dict(schema_version=1, product_reinstalled=False,
               installed_dependency_root=str(runtime),
               observation_exit_code=observation, native_exit_code=native,
               report_directory=str(reports),
               results_archive=str(results_archive), execution_errors=execution_errors,
               boundary='Actual installed receiver adapter native test plus read-only installed-client observation; not a whole application binary release verdict')
(reports / 'one-click-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
with zipfile.ZipFile(results_archive, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(reports.iterdir()):
        if path.is_file() and path.suffix.lower() in ('.json', '.md'):
            archive.write(path, arcname=path.name)
print('\nReports: ' + str(reports))
print('Result ZIP: ' + str(results_archive))
print('Installed-client observation exit code: ' + str(observation))
print('Installed-adapter native-window test exit code: ' + str(native))
print('No product upgrade was performed.')
raise SystemExit(native if native else (0 if observation == 0 else 2))
'''

WINDOWS_CMD = r'''@echo off
setlocal
cd /d "%~dp0"
set "SHARE_MONKEY_VALIDATION_RUNTIME=%LOCALAPPDATA%\Programs\Share Monkey"
if not "%~1"=="" set "SHARE_MONKEY_VALIDATION_RUNTIME=%~1"
if not exist "%SHARE_MONKEY_VALIDATION_RUNTIME%\Python\python.exe" (
  echo Existing Share Monkey Python was not found.
  echo Drag this CMD into a terminal and pass the installed Share Monkey folder as its first argument.
  pause
  exit /b 2
)
echo Share Monkey Windows own-window native validation
echo This test loads installed input adapters and does not upgrade the app.
echo Keep the desktop unlocked. Escape or switching windows stops native input.
"%SHARE_MONKEY_VALIDATION_RUNTIME%\Python\python.exe" "%~dp0_bootstrap_validation.py" --native --installed-root "%SHARE_MONKEY_VALIDATION_RUNTIME%"
set "SHARE_MONKEY_VALIDATION_RESULT=%ERRORLEVEL%"
echo Validation exit code: %SHARE_MONKEY_VALIDATION_RESULT%
pause
exit /b %SHARE_MONKEY_VALIDATION_RESULT%
'''

README = '''# Share Monkey Windows 一键自检包

解压整个 ZIP，双击 `运行 Windows 自检.cmd`。复用已安装 Share Monkey 的 Python 和依赖，**不用再安装 Python，也不会升级或修改客户端**。

1. 保持 Windows 桌面解锁，现有客户端可以保持连接。先只读观察 15 秒。
2. 随后出现独立测试窗口。暂勿操作键鼠；按 Esc、关闭或切换窗口立即停止。
3. 自动在本测试窗口验证固定按键、鼠标位置、左键和滚轮，结果保存在 `reports` 文件夹，并自动整理为 `Share-Monkey-Windows-Results-时间.zip`，需要反馈时只需发送这个结果包。

没有默认安装在 `%LOCALAPPDATA%\\Programs\\Share Monkey` 时，可在终端把实际安装目录作为 CMD 的第一个参数传入。

验收范围：实际已安装 Windows 接收端输入适配器的原生合成输入 + 已安装客户端的只读日志观察。测试工具保留自己的源码版本与指纹；报告单独记录实际加载的已安装版本、模块路径和函数哈希。输入适配器通过不能当成整个 EXE 生命周期或真实双机测试全部通过。

测试不创建网络连接、不读取密码或剪贴板、不启动全局键鼠监听，不修改系统设置、注册表、防火墙或开机启动。测试窗口可能使正在远控的产品安全返回本机，这属于正常焦点保护。

清理会释放本工具按下的键和鼠标按钮，恢复原鼠标位置。若 Windows 自检尚未执行，发布验收必须显示未执行，不能显示通过。
'''


def build(output, root=None):
    root = Path(root or ROOT).resolve()
    output = Path(output)
    required = ['tools/probe_windows_native.py', 'tools/validation_common.py',
                'tools/collect_paired_evidence.py', 'sharemouse/input.py',
                'sharemouse/windows_pointer.py']
    for name in required:
        if not (root / name).is_file():
            raise FileNotFoundError('Validation bundle is incomplete: ' + name)
    paths = set((root / 'sharemouse').rglob('*.py'))
    paths.update((root / 'tools').glob('*.py'))
    paths.update((root / 'tests').glob('*.py'))
    # Keep the same fingerprint inventory as validation_common.source_context.
    payload = {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(paths)}
    payload.update({'_bootstrap_validation.py': BOOTSTRAP.encode('utf-8'),
                    '运行 Windows 自检.cmd': WINDOWS_CMD.replace('\n', '\r\n').encode('utf-8'),
                    'README-请先阅读.md': README.encode('utf-8')})
    manifest = {'schema_version': 1, 'product_rebuilt': False,
                'dependency_runtime': 'Existing installed Share Monkey Python + pkgs',
                'files': [{'path': name, 'bytes': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(payload.items())]}
    payload['bundle-manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            archive.writestr(name, data)
    temporary.replace(output)
    return {'path': str(output.resolve()), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'bytes': output.stat().st_size, 'files': len(payload), 'product_rebuilt': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'validation' / 'Share-Monkey-Windows-Validation.zip')
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
