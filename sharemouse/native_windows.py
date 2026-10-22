"""Windows 11 desktop and notification area UI, backed by the shared session."""
import ctypes
from pathlib import Path
import sys

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from . import __version__
from .client import WINDOWS
from .display import enable_dpi_awareness
from .instance import AlreadyRunning, InstanceLock
from .session import Session, Settings
from .bluetooth_config import setup_help

ASSETS = Path(__file__).with_name('assets')


class ToggleSwitch(W.QAbstractButton):
    """Keyboard-accessible Fluent-style toggle without an extra UI dependency."""
    def __init__(self, title, checked=False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setAccessibleName(title)
        self.setCursor(C.Qt.PointingHandCursor)
        self.setFocusPolicy(C.Qt.StrongFocus)
        self.setFixedSize(44, 26)

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.Antialiasing)
        dark = getattr(self.window(), 'dark_mode', False)
        accent = G.QColor('#60cdff' if dark else '#0067c0')
        muted = G.QColor('#bcbcbc' if dark else '#606060')
        if not self.isEnabled():
            painter.setOpacity(.45)
        painter.setPen(G.QPen(accent if self.isChecked() else muted, 1.2))
        painter.setBrush(accent if self.isChecked() else C.Qt.NoBrush)
        painter.drawRoundedRect(C.QRectF(2, 3, 40, 20), 10, 10)
        painter.setPen(C.Qt.NoPen)
        painter.setBrush(G.QColor('#151515' if dark else '#ffffff') if self.isChecked() else muted)
        painter.drawEllipse(C.QRectF(25 if self.isChecked() else 6, 7, 12, 12))
        if self.hasFocus():
            painter.setBrush(C.Qt.NoBrush)
            painter.setPen(G.QPen(muted, 1, C.Qt.DotLine))
            painter.drawRoundedRect(C.QRectF(.5, .5, 43, 25), 5, 5)


class Desktop(W.QMainWindow):
    def __init__(self, client=WINDOWS, smoke=False, guard=None):
        super().__init__()
        self.client, self.guard = client, guard
        self.quitting = self.refreshing = False
        self.setWindowTitle('Share Monkey')
        self.setWindowIcon(G.QIcon(str(ASSETS / 'monkey.ico')))
        self.resize(1060, 780)
        self.setMinimumSize(880, 660)
        self.session = Session(client, self.refresh, discovery=not smoke,
                               settings=Settings(role='receiver', speed=1.) if smoke else None,
                               persist=not smoke)
        self.build()
        self.make_tray()
        self.refresh()
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.apply_theme()
        W.QApplication.instance().styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())

    def build(self):
        root = W.QWidget()
        self.setCentralWidget(root)
        layout = W.QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = W.QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(232)
        side = W.QVBoxLayout(sidebar)
        side.setContentsMargins(20, 32, 20, 24)
        side.setSpacing(12)
        icon = W.QLabel()
        icon.setPixmap(G.QIcon(str(ASSETS / 'monkey.svg')).pixmap(48, 48))
        side.addWidget(icon)
        brand = W.QLabel('Share Monkey')
        brand.setObjectName('brand')
        side.addWidget(brand)
        side.addWidget(self.caption('一套键鼠，流畅穿梭。'))
        side.addSpacing(28)
        self.nav = W.QButtonGroup(self)
        for i, text in enumerate(['连接与设备', '键鼠与屏幕', '共享设置']):
            button = W.QPushButton(text)
            button.setProperty('navigation', True)
            button.setCheckable(True)
            button.setMinimumHeight(42)
            self.nav.addButton(button, i)
            side.addWidget(button)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(self.navigate)
        side.addStretch()
        self.side_state = self.caption('●  本机可用')
        side.addWidget(self.side_state)
        side.addWidget(self.caption(f'版本 {__version__} · Windows'))
        layout.addWidget(sidebar)

        body = W.QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        layout.addLayout(body, 1)
        header = W.QWidget()
        header_layout = W.QVBoxLayout(header)
        header_layout.setContentsMargins(32, 29, 32, 20)
        header_layout.setSpacing(8)
        self.title = W.QLabel('连接与设备')
        self.title.setObjectName('pageTitle')
        header_layout.addWidget(self.title)
        self.subtitle = self.caption('同一网络，自动发现。Mac 与 Windows 从这里连接。')
        header_layout.addWidget(self.subtitle)
        body.addWidget(header)
        self.stack = W.QStackedWidget()
        body.addWidget(self.stack, 1)
        for builder in (self.build_connection, self.build_layout, self.build_sharing):
            scroll = W.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(W.QFrame.NoFrame)
            content = W.QWidget()
            column = W.QVBoxLayout(content)
            column.setContentsMargins(32, 2, 32, 24)
            column.setSpacing(16)
            builder(column)
            column.addStretch()
            scroll.setWidget(content)
            self.stack.addWidget(scroll)
        footer = W.QFrame()
        footer.setObjectName('footer')
        foot = W.QHBoxLayout(footer)
        foot.setContentsMargins(32, 18, 32, 18)
        status = W.QVBoxLayout()
        status.setSpacing(6)
        self.state = W.QLabel()
        self.state.setWordWrap(True)
        status.addWidget(self.state)
        status.addWidget(self.caption('关闭窗口后继续运行 · F12 随时返回本机'))
        foot.addLayout(status, 1)
        self.disconnect_button = W.QPushButton('断开')
        self.disconnect_button.clicked.connect(self.session.disconnect)
        foot.addWidget(self.disconnect_button)
        self.connect_button = W.QPushButton('开始等待')
        self.connect_button.setProperty('primary', True)
        self.connect_button.setMinimumWidth(126)
        self.connect_button.clicked.connect(self.connect_session)
        foot.addWidget(self.connect_button)
        body.addWidget(footer)

    def caption(self, text):
        label = W.QLabel(text)
        label.setProperty('secondary', True)
        label.setWordWrap(True)
        return label

    def card(self, column, title, detail=''):
        card = W.QFrame()
        card.setProperty('card', True)
        form = W.QVBoxLayout(card)
        form.setContentsMargins(22, 20, 22, 22)
        form.setSpacing(14)
        label = W.QLabel(title)
        label.setProperty('sectionTitle', True)
        form.addWidget(label)
        if detail:
            form.addWidget(self.caption(detail))
        column.addWidget(card)
        return form

    def build_connection(self, column):
        s = self.session.settings
        role = self.card(column, '连接方式与本机角色', '键鼠连接在哪台电脑，就让那台电脑作为控制端。')
        self.transport = W.QComboBox()
        self.transport.addItems(['局域网', '蓝牙直连'])
        self.transport.setCurrentIndex(1 if s.transport == 'bluetooth' else 0)
        self.transport.setAccessibleName('连接方式')
        self.transport.currentIndexChanged.connect(lambda i: self.configure(transport='bluetooth' if i else 'lan'))
        role.addWidget(self.transport)
        self.role = W.QComboBox()
        self.role.addItems(['使用本机键鼠', '接受另一台电脑控制'])
        self.role.setCurrentIndex(0 if s.role == 'controller' else 1)
        self.role.currentIndexChanged.connect(lambda i: self.configure(role='controller' if i == 0 else 'receiver'))
        role.addWidget(self.role)
        pair = self.card(column, '连接你的另一台电脑')
        self.devices = W.QComboBox()
        self.devices.setPlaceholderText('选择附近的电脑')
        self.devices.currentTextChanged.connect(self.select_peer)
        pair.addWidget(self.devices)
        self.discovery = self.caption('')
        pair.addWidget(self.discovery)
        self.bluetooth_hint = self.caption('先在 Mac 点击“开启蓝牙等待”，再选择上方的 Mac 并连接。无需在系统设置中预先配对。')
        pair.addWidget(self.bluetooth_hint)
        pair.addWidget(W.QLabel('配对密码'))
        self.password = W.QLineEdit()
        self.password.setEchoMode(W.QLineEdit.Password)
        self.password.setPlaceholderText('两台电脑使用相同密码，4 位数字即可')
        self.password.returnPressed.connect(self.connect_session)
        pair.addWidget(self.password)
        pair.addWidget(self.caption('密码仅在当前运行期间保留，用于断线后自动重连。'))
        row = W.QHBoxLayout()
        self.manual = W.QCheckBox('手动填写 IP 地址')
        self.manual.setChecked(s.manual)
        self.manual.toggled.connect(lambda value: self.configure(manual=value))
        row.addWidget(self.manual)
        row.addStretch()
        self.help_button = W.QPushButton('连接帮助')
        self.help_button.clicked.connect(self.help)
        row.addWidget(self.help_button)
        pair.addLayout(row)
        self.manual_row = W.QWidget()
        manual = W.QHBoxLayout(self.manual_row)
        manual.setContentsMargins(0, 0, 0, 0)
        self.host = W.QLineEdit(s.host)
        self.host.setPlaceholderText('局域网 IP，例如 192.168.1.10')
        self.port = W.QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(s.port)
        manual.addWidget(self.host, 1)
        manual.addWidget(self.port)
        pair.addWidget(self.manual_row)
        self.bluetooth_limits = self.caption('蓝牙支持键鼠与 8 KB 内文本剪贴板；文件和长文本请使用局域网。')
        pair.addWidget(self.bluetooth_limits)

    def build_layout(self, column):
        s = self.session.settings
        layout = self.card(column, '屏幕排列', '选择另一台电脑所在的方向，鼠标移到对应边缘即可跨屏。')
        screens = W.QHBoxLayout()
        screens.setContentsMargins(36, 14, 36, 14)
        self.screen_labels = []
        for index in range(2):
            if index:
                arrow = W.QLabel('↔')
                arrow.setAlignment(C.Qt.AlignCenter)
                screens.addWidget(arrow)
            screen = W.QLabel()
            screen.setProperty('monitor', True)
            screen.setAlignment(C.Qt.AlignCenter)
            screen.setMinimumSize(160, 104)
            self.screen_labels.append(screen)
            screens.addWidget(screen, 1)
        layout.addLayout(screens)
        self.side = W.QComboBox()
        self.side.addItems(['另一台电脑在左侧', '另一台电脑在右侧'])
        self.side.setCurrentIndex(1 if s.side == 'right' else 0)
        self.side.currentIndexChanged.connect(lambda i: self.configure(side='right' if i else 'left'))
        layout.addWidget(self.side)
        speed = self.card(column, '鼠标速度', '按屏幕 DPI 换算；连接时也可以即时调整。')
        row = W.QHBoxLayout()
        self.speed = W.QSlider(C.Qt.Horizontal)
        self.speed.setRange(25, 400)
        self.speed.setValue(round(s.speed * 100))
        self.speed.valueChanged.connect(lambda value: self.session.set_speed(value / 100))
        row.addWidget(self.speed, 1)
        self.speed_value = W.QLabel(f'{s.speed:.0%}')
        self.speed_value.setMinimumWidth(54)
        row.addWidget(self.speed_value)
        speed.addLayout(row)
        speed.addWidget(self.caption('25% · 更精细                                                     400% · 更快速'))

    def build_sharing(self, column):
        self.switches = {}
        for key, title, detail in [('clipboard', '共享剪贴板', '在两台电脑间复制和粘贴文本。'),
                                   ('files', '允许接收文件', '接收的文件保存到“下载 / Share Monkey”。'),
                                   ('mapping', '快捷键适配', '在 Mac 与 Windows 间映射 Command 和 Control。'),
                                   ('auto_reconnect', '断线自动恢复', '暂时断线后自动重新连接，点击“断开”可停止。'),
                                   ('keep_awake', '关屏保持共享', '允许屏幕关闭；共享期间请求后台继续运行、减少自动睡眠。')]:
            card = self.card(column, title)
            row = W.QHBoxLayout()
            row.addWidget(self.caption(detail), 1)
            switch = ToggleSwitch(title, getattr(self.session.settings, key))
            switch.toggled.connect(lambda value, k=key: self.configure(**{k: value}))
            row.addWidget(switch)
            card.addLayout(row)
            self.switches[key] = switch
        column.addWidget(self.caption('共享设置在下次连接时生效。'))
        self.file_button = W.QPushButton('选择文件并发送…')
        self.file_button.clicked.connect(self.send_file)
        column.addWidget(self.file_button, alignment=C.Qt.AlignLeft)
        self.file_notice = self.caption('单个文件最大 100 MB，对方需允许接收。')
        column.addWidget(self.file_notice)

    def navigate(self, index):
        self.nav.button(index).setChecked(True)
        self.stack.setCurrentIndex(index)
        self.title.setText(['连接与设备', '键鼠与屏幕', '共享设置'][index])
        self.subtitle.setText([self.connection_subtitle(),
                               '让指针的方向和速度，都符合你的习惯。', '让电脑之间的工作自然衔接。'][index])

    def connection_subtitle(self):
        return ('无需网络。先在 Mac 开启蓝牙等待，再选择附近的 Mac。'
                if self.session.settings.transport == 'bluetooth' else
                '同一网络，自动发现。Mac 与 Windows 从这里连接。')

    def make_tray(self):
        self.tray = W.QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('Share Monkey · 键鼠共享')
        menu = W.QMenu()
        heading = menu.addAction('Share Monkey')
        heading.setEnabled(False)
        self.tray_state = menu.addAction('准备就绪')
        self.tray_state.setEnabled(False)
        menu.addSeparator()
        menu.addAction('打开主窗口', self.show_window)
        self.tray_disconnect = menu.addAction('断开连接', self.session.disconnect)
        menu.addSeparator()
        menu.addAction('退出 Share Monkey', self.quit)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (W.QSystemTrayIcon.Trigger, W.QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.quitting:
            event.accept()
        elif W.QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            # Keep a visible recovery path if Explorer / the notification area is unavailable.
            event.ignore()
            self.showMinimized()

    def tick(self):
        self.session.poll()
        if self.guard and self.guard.consume_show_request():
            self.show_window()

    def configure(self, **values):
        if self.refreshing:
            return
        try:
            self.session.configure(**values)
        except Exception as exc:
            self.refresh()
            W.QMessageBox.warning(self, '无法修改设置', str(exc))

    def select_peer(self, label):
        if not self.refreshing:
            self.session.selected = label

    def connect_session(self):
        try:
            if self.session.settings.manual and self.session.settings.transport == 'lan':
                self.session.settings.host = self.host.text()
                self.session.settings.port = self.port.value()
            self.session.connect(self.password.text(), int(self.winId()))
        except Exception as exc:
            W.QMessageBox.warning(self, '无法连接', str(exc))

    def send_file(self):
        filename, _ = W.QFileDialog.getOpenFileName(self, '选择发送的文件（最多 100 MB）')
        if filename:
            try:
                self.session.send_file(filename)
            except Exception as exc:
                W.QMessageBox.warning(self, '无法发送文件', str(exc))

    def help(self):
        bluetooth = self.session.settings.transport == 'bluetooth'
        if bluetooth:
            self.bluetooth_help_dialog().exec()
        else:
            W.QMessageBox.information(self, '连接帮助', self.client.help)

    def bluetooth_help_dialog(self):
        dialog = W.QDialog(self)
        dialog.setWindowTitle('蓝牙配置 · Share Monkey')
        dialog.setWindowIcon(self.windowIcon())
        screen = self.screen() or W.QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else C.QRect(0, 0, 1024, 768)
        dialog.resize(min(660, max(380, available.width() - 120)),
                      min(680, max(360, available.height() - 120)))
        dialog.setMinimumSize(380, 360)
        layout = W.QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)
        title = W.QLabel('蓝牙直连配置')
        title.setProperty('sectionTitle', True)
        layout.addWidget(title)
        layout.addWidget(self.caption('按照下方步骤连接。说明可以滚动，也可以选择文字复制。'))
        text = W.QTextEdit()
        text.setObjectName('bluetoothHelpText')
        text.setReadOnly(True)
        text.setAcceptRichText(False)
        text.setTabChangesFocus(True)
        text.setLineWrapMode(W.QTextEdit.WidgetWidth)
        text.setPlainText(setup_help('win32'))
        text.setAccessibleName('蓝牙直连配置说明')
        layout.addWidget(text, 1)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Close)
        buttons.button(W.QDialogButtonBox.Close).setText('完成')
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog

    def refresh(self):
        if not hasattr(self, 'connect_button') or self.quitting:
            return
        self.refreshing = True
        try:
            s = self.session.settings
            bluetooth = s.transport == 'bluetooth'
            running, connected = self.session.running, self.session.connected
            self.state.setText(self.session.status)
            self.connect_button.setText('连接 Mac' if bluetooth else '连接' if s.role == 'controller' else '开始等待')
            self.connect_button.setEnabled(not running)
            self.disconnect_button.setEnabled(running)
            self.tray_disconnect.setEnabled(running)
            self.tray_state.setText(self.session.status)
            self.tray.setToolTip('Share Monkey · ' + self.session.status)
            self.side_state.setText('●  已连接' if connected else '●  正在重连' if self.session.reconnecting else '●  等待连接' if running else '●  本机可用')
            self.manual.setVisible(not bluetooth)
            self.manual_row.setVisible(s.manual and not bluetooth)
            self.devices.setVisible(bluetooth or (not s.manual and s.role == 'controller'))
            self.discovery.setText('手动连接使用同一网络中的 IP 地址。' if s.manual and not bluetooth else self.session.discovery_status)
            self.bluetooth_hint.setVisible(bluetooth)
            self.bluetooth_limits.setVisible(bluetooth)
            self.help_button.setText('蓝牙配置' if bluetooth else '连接帮助')
            self.transport.setCurrentIndex(1 if bluetooth else 0)
            self.role.setCurrentIndex(0 if s.role == 'controller' else 1)
            self.manual.setChecked(s.manual)
            self.side.setCurrentIndex(1 if s.side == 'right' else 0)
            self.devices.setPlaceholderText('选择附近的 Mac' if bluetooth else '选择附近的电脑')
            if self.stack.currentIndex() == 0:
                self.subtitle.setText(self.connection_subtitle())
            values = list(self.session.peers)
            current = [self.devices.itemText(i) for i in range(self.devices.count())]
            if current != values:
                self.devices.clear()
                self.devices.addItems(values)
            self.devices.setCurrentIndex(values.index(self.session.selected) if self.session.selected in values else -1)
            for widget in [self.transport, self.role, self.manual, self.devices, self.host, self.port, self.side, *self.switches.values()]:
                widget.setEnabled(not running)
            self.switches['files'].setEnabled(not running and not bluetooth)
            for key, switch in self.switches.items():
                switch.setChecked(bool(getattr(s, key)) and not (bluetooth and key == 'files'))
            self.file_button.setEnabled(connected and not bluetooth)
            self.file_notice.setText('蓝牙模式不传输文件，请切换为局域网。' if bluetooth else
                                     '单个文件最大 100 MB，对方需允许接收。')
            names = ['Windows · 本机', self.client.peer] if s.side == 'right' else [self.client.peer, 'Windows · 本机']
            for view, text in zip(self.screen_labels, names):
                view.setText(text + '\n\n显示器')
            self.speed_value.setText(f'{s.speed:.0%}')
        finally:
            self.refreshing = False

    def apply_theme(self, dark=None):
        if dark is None:
            dark = W.QApplication.instance().styleHints().colorScheme() == C.Qt.ColorScheme.Dark
            if sys.platform == 'win32':
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                        r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                        dark = not bool(winreg.QueryValueEx(key, 'AppsUseLightTheme')[0])
                except OSError:
                    pass
        self.dark_mode = dark
        bg, sidebar, card, ink, muted, border = (('#202020', '#282828', '#2d2d2d', '#fafafa', '#bcbcbc', '#424242')
                                               if dark else ('#f3f3f3', '#eeeeee', '#ffffff', '#1a1a1a', '#606060', '#e2e2e2'))
        accent, hover = ('#60cdff', '#363636') if dark else ('#0067c0', '#f5f5f5')
        palette = G.QPalette()
        for role, color in [(G.QPalette.Window, bg), (G.QPalette.Base, card),
                            (G.QPalette.AlternateBase, sidebar), (G.QPalette.WindowText, ink),
                            (G.QPalette.Text, ink), (G.QPalette.Button, card),
                            (G.QPalette.ButtonText, ink), (G.QPalette.Highlight, accent),
                            (G.QPalette.HighlightedText, '#151515' if dark else '#ffffff')]:
            palette.setColor(role, G.QColor(color))
        W.QApplication.instance().setPalette(palette)
        self.setStyleSheet(f'''
            QWidget {{ color: {ink}; }}
            QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {bg}; }}
            #sidebar {{ background: {sidebar}; border-right: 1px solid {border}; }}
            #brand {{ font-size: 21px; font-weight: 600; }}
            #pageTitle {{ font-size: 29px; font-weight: 600; }}
            QLabel[secondary="true"] {{ color: {muted}; font-size: 12px; }}
            QLabel[sectionTitle="true"] {{ font-size: 16px; font-weight: 600; }}
            QFrame[card="true"] {{ background: {card}; border: 1px solid {border}; border-radius: 10px; }}
            QLabel[monitor="true"] {{ background: {bg}; border: 2px solid {accent}; border-radius: 10px; font-size: 16px; }}
            #footer {{ background: {bg}; border-top: 1px solid {border}; }}
            QPushButton {{ background: {card}; border: 1px solid {border}; border-radius: 6px; padding: 8px 16px; min-height: 18px; }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:disabled {{ color: {muted}; background: {bg}; }}
            QPushButton[primary="true"] {{ background: {accent}; color: {'#151515' if dark else 'white'}; border-color: {accent}; }}
            QPushButton[primary="true"]:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
            QPushButton[navigation="true"] {{ text-align: left; background: transparent; border: 0; padding: 10px 16px; }}
            QPushButton[navigation="true"]:checked {{ background: {card}; border-left: 3px solid {accent}; }}
            QLineEdit, QComboBox, QSpinBox {{ background: {card}; border: 1px solid {border}; border-bottom: 1px solid {muted}; border-radius: 5px; padding: 8px 10px; min-height: 20px; }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-bottom: 2px solid {accent}; }}
            QComboBox::drop-down {{ border: 0; width: 30px; }}
            QComboBox::down-arrow {{ image: url("{(ASSETS / 'chevron.svg').as_posix()}"); width: 12px; height: 12px; }}
            QComboBox QAbstractItemView {{ background: {card}; selection-background-color: {accent}; }}
            QTextEdit#bluetoothHelpText {{ background: {card}; border: 1px solid {border}; border-radius: 8px; padding: 12px; }}
            QCheckBox {{ spacing: 8px; min-height: 28px; }}
            QSlider::groove:horizontal {{ height: 4px; background: {border}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {accent}; border: 3px solid {card}; width: 14px; margin: -6px 0; border-radius: 10px; }}
            QToolTip {{ background: {card}; color: {ink}; border: 1px solid {border}; }}
        ''')
        for switch in self.switches.values():
            switch.update()
        if sys.platform == 'win32':
            try:
                hwnd = int(self.winId())
                dwm = ctypes.windll.dwmapi
                for attribute, value in [(20, int(dark)), (33, 2), (38, 2)]:
                    data = ctypes.c_int(value)
                    dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attribute, ctypes.byref(data), 4)
            except (AttributeError, OSError):
                pass

    def prepare_exit(self):
        if self.quitting:
            return
        self.quitting = True
        self.timer.stop()
        self.session.close()
        self.tray.hide()

    def quit(self):
        self.prepare_exit()
        W.QApplication.instance().quit()


def configure_fonts(app):
    app.setStyle('Fusion')
    font = G.QFont()
    font.setFamilies(['Segoe UI Variable Text', 'Segoe UI Variable', 'Segoe UI', 'Microsoft YaHei UI'])
    font.setPointSizeF(10.5)
    app.setFont(font)


def main(client=WINDOWS):
    enable_dpi_awareness()
    if sys.platform == 'win32':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('local.ShareMonkey.Desktop')
    app = W.QApplication.instance() or W.QApplication(sys.argv)
    app.setApplicationName('Share Monkey')
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    configure_fonts(app)
    smoke = '--smoke-test' in sys.argv
    guard = None
    if not smoke:
        guard = InstanceLock()
        try:
            guard.acquire()
        except AlreadyRunning:
            guard.request_show()
            return
    window = Desktop(client, smoke, guard)
    app.aboutToQuit.connect(window.prepare_exit)
    app.commitDataRequest.connect(lambda manager: window.prepare_exit())
    window.show()
    if smoke:
        C.QTimer.singleShot(600, window.quit)
    try:
        app.exec()
    finally:
        window.session.close()
        window.tray.hide()
        if guard:
            guard.close()


if __name__ == '__main__':
    main()
