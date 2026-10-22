"""Preview a platform desktop without discovery or input hooks."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
parser = argparse.ArgumentParser()
parser.add_argument('client', choices=('macos', 'windows'))
parser.add_argument('--snapshot', type=Path)
args = parser.parse_args()
if args.client == 'macos':
    from sharemouse.native_mac import Desktop, A, F
    desktop = Desktop.alloc().init().setup(smoke=True)
    desktop.show_window()
    def capture(timer):
        if args.snapshot:
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            desktop.window.displayIfNeeded()
            desktop.snapshot(args.snapshot)
        desktop.quit()
    F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(.5 if args.snapshot else 45., False, capture)
    desktop.app.run()
else:
    from PySide6 import QtCore as C, QtWidgets as W
    from sharemouse.native_windows import Desktop, configure_fonts
    app = W.QApplication([])
    configure_fonts(app)
    desktop = Desktop(smoke=True)
    desktop.show()
    def capture():
        if args.snapshot:
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            desktop.grab().save(str(args.snapshot))
        desktop.quit()
    C.QTimer.singleShot(500 if args.snapshot else 45000, capture)
    app.exec()
