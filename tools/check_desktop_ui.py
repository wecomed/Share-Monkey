"""Exercise each desktop without network, input hooks, or saved user preferences."""
import argparse
import json
from pathlib import Path
import sys
import threading
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fake_engine():
    engine = Mock()
    engine.stop_event = threading.Event()
    engine.connected = True
    return engine


def check_mac(output):
    from sharemouse.native_mac import Desktop, A, F
    from sharemouse.discovery import Peer
    desktop = Desktop.alloc().init().setup(smoke=True)
    errors = []
    def verify(timer):
        try:
            desktop.app.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
            peer = Peer('192.168.1.8', 24809, '工作电脑', 'Windows')
            desktop.session.peers = {peer.label: peer}
            desktop.session.selected = peer.label
            desktop.session.discovery_status = '发现 1 台电脑 · 选择设备即可连接'
            desktop.refresh()
            for index, name in enumerate(('connection', 'layout', 'sharing')):
                desktop.navigate(index)
                desktop.window.displayIfNeeded()
                desktop.snapshot(output / f'mac-{name}.png')
            desktop.app.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameDarkAqua))
            desktop.navigate(0)
            desktop.window.displayIfNeeded()
            desktop.snapshot(output / 'mac-dark.png')
            desktop.app.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
            desktop.session.configure(files=True, manual=True, transport='bluetooth')
            assert desktop.connect_button.title() == '开启蓝牙等待'
            assert desktop.manual.isHidden() and desktop.devices.isHidden()
            assert not desktop.bluetooth_hint.isHidden()
            assert not desktop.switches['files'].isEnabled() and not desktop.switches['files'].state()
            assert desktop.session.settings.files
            desktop.navigate(0)
            desktop.window.displayIfNeeded()
            desktop.snapshot(output / 'mac-bluetooth.png')
            guide = desktop.bluetooth_help_alert()
            scroll = guide.accessoryView()
            assert scroll.hasVerticalScroller() and not scroll.documentView().isEditable()
            assert '开启蓝牙等待' in str(scroll.documentView().string())
            assert 'NSBluetoothAlwaysUsageDescription' in str(scroll.documentView().string())
            desktop.session.configure(transport='lan', manual=False)
            assert desktop.switches['files'].isEnabled() and desktop.switches['files'].state()
            desktop.session.engine = engine = fake_engine()
            desktop.refresh()
            assert desktop.activity is not None
            for _ in range(30):
                desktop.window.performClose_(None)
                assert not desktop.window.isVisible()
                assert desktop.status_item.isVisible()
                assert desktop.session.connected and not desktop.session.closed
                desktop.show_window()
                assert desktop.window.isVisible()
            engine.stop.assert_not_called()
            assert desktop.tray_disconnect.isEnabled()
            assert not desktop.connect_button.isEnabled()
            desktop.session.disconnect()
            engine.stop.assert_called_once()
            assert desktop.activity is None
            desktop.window.setContentSize_(A.NSMakeSize(870, 660))
            desktop.navigate(0)
            desktop.window.displayIfNeeded()
            desktop.snapshot(output / 'mac-small.png')
            assert desktop.connect_button.frame().origin.y >= 0
            assert desktop.connect_button.superview().frame().origin.y < desktop.root.bounds().size.height
            (output / 'mac-ui.json').write_text(json.dumps({
                'platform': sys.platform, 'native': 'AppKit', 'hide_show_cycles': 30,
                'engine': 'test double; no network or input hooks', 'status_item': True,
                'app_nap_activity': True, 'quit_and_disconnect': True,
                'system_font': str(desktop.page_title.font().fontName()),
                'glass_navigation': hasattr(A, 'NSGlassEffectView'),
                'bluetooth_mode_and_scrollable_help': True,
            }, indent=2), encoding='utf-8')
            print('PASS: AppKit pages, dark mode, resize, 30 close/reopen cycles, menu bar, activity, disconnect', flush=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            desktop.quit()
    desktop.show_window()
    F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(.5, False, verify)
    desktop.app.run()
    if errors:
        raise errors[0]


def check_windows(output):
    from PySide6 import QtCore as C, QtWidgets as W
    from sharemouse.native_windows import Desktop, configure_fonts
    app = W.QApplication.instance() or W.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    configure_fonts(app)
    desktop = Desktop(smoke=True)
    desktop.show()
    app.processEvents()
    for index, name in enumerate(('connection', 'layout', 'sharing')):
        desktop.navigate(index)
        app.processEvents()
        desktop.grab().save(str(output / f'windows-{name}.png'))
    desktop.apply_theme(True)
    app.processEvents()
    desktop.navigate(0)
    desktop.grab().save(str(output / 'windows-dark.png'))
    desktop.apply_theme(False)
    app.processEvents()
    desktop.session.configure(files=True, manual=True, role='receiver', transport='bluetooth')
    app.processEvents()
    assert desktop.connect_button.text() == '连接 Mac'
    assert not desktop.manual.isVisible() and not desktop.manual_row.isVisible()
    assert desktop.devices.isVisible() and desktop.bluetooth_hint.isVisible()
    assert not desktop.switches['files'].isEnabled() and not desktop.switches['files'].isChecked()
    assert desktop.session.settings.files
    desktop.grab().save(str(output / 'windows-bluetooth.png'))
    guide = desktop.bluetooth_help_dialog()
    guide.show()
    app.processEvents()
    text = guide.findChild(W.QTextEdit, 'bluetoothHelpText')
    assert text.isReadOnly() and text.verticalScrollBar().maximum() > 0
    assert '开启蓝牙等待' in text.toPlainText()
    guide.grab().save(str(output / 'windows-bluetooth-help.png'))
    guide.close()
    desktop.session.configure(transport='lan', manual=False)
    app.processEvents()
    assert desktop.switches['files'].isEnabled() and desktop.switches['files'].isChecked()
    desktop.session.engine = engine = fake_engine()
    desktop.refresh()
    with patch.object(W.QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
        for _ in range(30):
            desktop.close()
            app.processEvents()
            assert not desktop.isVisible()
            assert desktop.tray.isVisible()
            assert desktop.session.connected and not desktop.session.closed
            desktop.show_window()
            app.processEvents()
            assert desktop.isVisible()
    engine.stop.assert_not_called()
    with patch.object(W.QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
        desktop.close()
        app.processEvents()
        assert desktop.isMinimized() and not desktop.session.closed
    desktop.show_window()
    desktop.session.disconnect()
    engine.stop.assert_called_once()
    desktop.resize(880, 660)
    app.processEvents()
    desktop.grab().save(str(output / 'windows-small.png'))
    assert desktop.connect_button.isVisible()
    desktop.quit()
    assert desktop.session.closed and not desktop.tray.isVisible()
    (output / 'windows-ui.json').write_text(json.dumps({
        'host_platform': sys.platform, 'native_windows_runtime': sys.platform == 'win32',
        'toolkit': 'PySide6 / Qt Widgets', 'hide_show_cycles': 30,
        'engine': 'test double; no network or input hooks',
        'tray_missing_fallback': 'minimize', 'quit_and_disconnect': True,
        'bluetooth_mode_and_scrollable_help': True,
    }, indent=2), encoding='utf-8')
    print('PASS: Windows Qt pages, dark mode, resize, 30 close/reopen cycles, tray fallback, disconnect')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('client', choices=('macos', 'windows'))
    parser.add_argument('--output', type=Path, default=ROOT / 'validation' / 'v053' / 'ui')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (check_mac if args.client == 'macos' else check_windows)(args.output.resolve())
