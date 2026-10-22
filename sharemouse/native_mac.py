"""AppKit desktop and menu bar client. Input capture remains in the existing engine."""
from pathlib import Path
import sys

import AppKit as A
import Foundation as F
import objc

from . import __version__
from .client import MAC
from .instance import AlreadyRunning, InstanceLock
from .session import Session, Settings
from .bluetooth_config import setup_help
from .mac_capture_ui import get_capture_coordinator


class FlippedView(A.NSView):
    def isFlipped(self):
        return True


class BackgroundView(FlippedView):
    def isOpaque(self):
        return True

    def drawRect_(self, rect):
        A.NSColor.windowBackgroundColor().setFill()
        A.NSRectFill(rect)


def label(text, size=13, weight=A.NSFontWeightRegular, secondary=False):
    view = A.NSTextField.labelWithString_(text)
    view.setFont_(A.NSFont.systemFontOfSize_weight_(size, weight))
    view.setTextColor_(A.NSColor.secondaryLabelColor() if secondary else A.NSColor.labelColor())
    view.setLineBreakMode_(A.NSLineBreakByTruncatingTail)
    return view


def place(parent, view, x, y, width, height, autoresize=A.NSViewWidthSizable):
    view.setFrame_(A.NSMakeRect(x, y, width, height))
    view.setAutoresizingMask_(autoresize)
    parent.addSubview_(view)
    return view


class Desktop(A.NSObject):
    @objc.python_method
    def setup(self, client=MAC, smoke=False, guard=None):
        self.client, self.smoke, self.guard = client, smoke, guard
        self.actions = {}
        self.next_action = 1
        self.closed = False
        self.window_open_requested = False
        self.activity = None
        self.page = 0
        self.app = A.NSApplication.sharedApplication()
        self.app.setDelegate_(self)
        self.app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
        self.capture_ui = get_capture_coordinator()
        self.session = Session(client, self.refresh, discovery=not smoke,
                               settings=Settings() if smoke else None, persist=not smoke)
        self.make_menu()
        self.make_window()
        self.make_status_item()
        self.timer = F.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            .1, self, 'tick:', None, True)
        F.NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, F.NSRunLoopCommonModes)
        self.refresh()
        return self

    @objc.python_method
    def bind(self, control, callback):
        tag = self.next_action
        self.next_action += 1
        self.actions[tag] = callback
        control.setTag_(tag)
        control.setTarget_(self)
        control.setAction_('action:')
        return control

    def action_(self, sender):
        try:
            self.actions[sender.tag()]()
        except Exception as exc:
            self.refresh()
            self.alert(str(exc))

    @objc.python_method
    def button(self, title, callback, primary=False):
        button = A.NSButton.buttonWithTitle_target_action_(title, self, 'action:')
        button.setBezelStyle_(A.NSBezelStyleRounded)
        button.setFont_(A.NSFont.systemFontOfSize_weight_(13, A.NSFontWeightMedium))
        if primary:
            button.setBezelColor_(A.NSColor.controlAccentColor())
            button.setKeyEquivalent_('\r')
        return self.bind(button, callback)

    @objc.python_method
    def menu_item(self, menu, title, callback, key=''):
        item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, 'action:', key)
        self.bind(item, callback)
        menu.addItem_(item)
        return item

    @objc.python_method
    def make_menu(self):
        bar = A.NSMenu.alloc().init()
        parent = A.NSMenuItem.alloc().init()
        bar.addItem_(parent)
        menu = A.NSMenu.alloc().initWithTitle_('Share Monkey')
        self.menu_item(menu, '关于 Share Monkey', self.about)
        menu.addItem_(A.NSMenuItem.separatorItem())
        self.menu_item(menu, '显示 Share Monkey', self.show_window, '0')
        self.menu_item(menu, '隐藏窗口', self.hide_window, 'w')
        menu.addItem_(A.NSMenuItem.separatorItem())
        self.menu_item(menu, '退出 Share Monkey', self.quit, 'q')
        parent.setSubmenu_(menu)
        edit_parent = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('编辑', None, '')
        edit_menu = A.NSMenu.alloc().initWithTitle_('编辑')
        for title, action, key in [('撤销', 'undo:', 'z'), ('剪切', 'cut:', 'x'),
                                   ('复制', 'copy:', 'c'), ('粘贴', 'paste:', 'v'), ('全选', 'selectAll:', 'a')]:
            edit_menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        edit_parent.setSubmenu_(edit_menu)
        bar.addItem_(edit_parent)
        self.app.setMainMenu_(bar)

    @objc.python_method
    def make_window(self):
        style = (A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable |
                 A.NSWindowStyleMaskMiniaturizable | A.NSWindowStyleMaskResizable |
                 A.NSWindowStyleMaskFullSizeContentView)
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, 980, 740), style, A.NSBackingStoreBuffered, False)
        self.window.setTitle_('Share Monkey')
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(A.NSWindowTitleHidden)
        self.window.setMinSize_(A.NSMakeSize(870, 660))
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.window.setBackgroundColor_(A.NSColor.windowBackgroundColor())
        self.window.center()
        self.root = BackgroundView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 980, 740))
        self.window.setContentView_(self.root)

        sidebar = A.NSVisualEffectView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 224, 740))
        sidebar.setMaterial_(A.NSVisualEffectMaterialSidebar)
        sidebar.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        sidebar.setState_(A.NSVisualEffectStateFollowsWindowActiveState)
        sidebar.setAutoresizingMask_(A.NSViewHeightSizable)
        self.root.addSubview_(sidebar)
        self.sidebar = FlippedView.alloc().initWithFrame_(sidebar.bounds())
        self.sidebar.setAutoresizingMask_(A.NSViewWidthSizable | A.NSViewHeightSizable)
        sidebar.addSubview_(self.sidebar)
        image = A.NSImage.alloc().initWithContentsOfFile_(str(Path(__file__).with_name('assets') / 'monkey.png'))
        icon = A.NSImageView.alloc().init()
        icon.setImage_(image)
        icon.setContentTintColor_(A.NSColor.controlAccentColor())
        place(self.sidebar, icon, 24, 70, 36, 36, 0)
        place(self.sidebar, label('Share Monkey', 19, A.NSFontWeightSemibold), 24, 116, 188, 27, 0)
        place(self.sidebar, label('一套键鼠，流畅穿梭。', 12, secondary=True), 24, 147, 180, 20, 0)
        self.nav = []
        self.nav_backgrounds = []
        nav_parent = self.sidebar
        nav_origin = 206
        if hasattr(A, 'NSGlassEffectView'):
            glass = A.NSGlassEffectView.alloc().init()
            glass.setStyle_(A.NSGlassEffectViewStyleRegular)
            glass.setCornerRadius_(16)
            nav_parent = FlippedView.alloc().init()
            glass.setContentView_(nav_parent)
            place(self.sidebar, glass, 12, 194, 200, 154, 0)
            nav_origin = 12
        for index, (title, symbol) in enumerate([('连接与设备', 'network'),
                                                ('键鼠与屏幕', 'display.2'),
                                                ('共享设置', 'square.on.square')]):
            button = self.button(title, lambda i=index: self.navigate(i))
            highlight = A.NSBox.alloc().init()
            highlight.setBoxType_(A.NSBoxCustom)
            highlight.setTitlePosition_(A.NSNoTitle)
            highlight.setFillColor_(A.NSColor.controlAccentColor().colorWithAlphaComponent_(.14))
            highlight.setBorderWidth_(0)
            highlight.setCornerRadius_(10)
            highlight.setHidden_(index != 0)
            place(nav_parent, highlight, 4 if nav_origin == 12 else 16, nav_origin + index * 48, 192, 38, 0)
            self.nav_backgrounds.append(highlight)
            button.setBordered_(False)
            button.setAlignment_(A.NSTextAlignmentLeft)
            button.setImage_(A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, title))
            button.setImagePosition_(A.NSImageLeading)
            place(nav_parent, button, 4 if nav_origin == 12 else 16, nav_origin + index * 48, 192, 38, 0)
            self.nav.append(button)
        self.sidebar_status = place(self.sidebar, label('●  本机可用', 12, secondary=True),
                                    24, 646, 184, 22, A.NSViewMinYMargin)
        place(self.sidebar, label(f'版本 {__version__} · macOS', 11, secondary=True),
              24, 675, 184, 20, A.NSViewMinYMargin)

        self.main = place(self.root, FlippedView.alloc().init(), 224, 34, 756, 620,
                          A.NSViewWidthSizable | A.NSViewHeightSizable)
        self.page_title = place(self.main, label('连接与设备', 26, A.NSFontWeightBold), 32, 24, 640, 36)
        self.page_subtitle = place(self.main, label('同一网络，自动发现。Mac 与 Windows 从这里连接。',
                                                   13, secondary=True), 32, 68, 660, 24)
        self.scroll = A.NSScrollView.alloc().init()
        self.scroll.setDrawsBackground_(False)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        place(self.main, self.scroll, 24, 110, 708, 504,
              A.NSViewWidthSizable | A.NSViewHeightSizable)
        self.pages = []
        for _ in range(3):
            view = FlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 690, 500))
            view.setAutoresizingMask_(A.NSViewWidthSizable)
            self.pages.append(view)
        self.build_connection()
        self.build_layout()
        self.build_sharing()
        self.scroll.setDocumentView_(self.pages[0])

        footer = place(self.root, FlippedView.alloc().init(), 224, 654, 756, 86,
                       A.NSViewWidthSizable | A.NSViewMinYMargin)
        line = A.NSBox.alloc().init()
        line.setBoxType_(A.NSBoxSeparator)
        place(footer, line, 0, 0, 756, 1)
        self.state_label = place(footer, label('准备就绪', 12), 32, 13, 390, 20)
        place(footer, label('关闭窗口后继续运行 · F12 随时返回本机', 11, secondary=True), 32, 39, 400, 20)
        self.disconnect_button = place(footer, self.button('断开', self.session.disconnect),
                                       500, 23, 80, 34, A.NSViewMinXMargin)
        self.connect_button = place(footer, self.button('连接', self.connect, True),
                                    590, 23, 136, 34, A.NSViewMinXMargin)

    @objc.python_method
    def card(self, page, y, height, title, detail=''):
        card = A.NSBox.alloc().init()
        card.setBoxType_(A.NSBoxCustom)
        card.setTitlePosition_(A.NSNoTitle)
        card.setFillColor_(A.NSColor.controlBackgroundColor())
        card.setBorderColor_(A.NSColor.separatorColor())
        card.setBorderWidth_(.5)
        card.setCornerRadius_(14)
        card.setContentViewMargins_(A.NSMakeSize(0, 0))
        body = FlippedView.alloc().init()
        card.setContentView_(body)
        place(page, card, 8, y, 674, height)
        place(body, label(title, 15, A.NSFontWeightSemibold), 22, 18, 620, 24)
        if detail:
            place(body, label(detail, 12, secondary=True), 22, 48, 620, 22)
        return body

    @objc.python_method
    def segmented(self, labels, selected, callback):
        control = A.NSSegmentedControl.alloc().init()
        control.setSegmentCount_(len(labels))
        control.setSegmentStyle_(A.NSSegmentStyleRounded)
        for index, text in enumerate(labels):
            control.setLabel_forSegment_(text, index)
        control.setSelectedSegment_(selected)
        self.bind(control, lambda: callback(control.selectedSegment()))
        return control

    @objc.python_method
    def build_connection(self):
        s = self.session.settings
        page = self.pages[0]
        page.setFrameSize_(A.NSMakeSize(690, 548))
        card = self.card(page, 4, 188, '连接方式与本机角色')
        self.transport = self.segmented(['局域网', '蓝牙直连'],
                                       1 if s.transport == 'bluetooth' else 0,
                                       lambda i: self.session.configure(transport='bluetooth' if i else 'lan'))
        self.transport.setAccessibilityLabel_('连接方式')
        place(card, self.transport, 22, 54, 628, 32)
        place(card, label('键鼠连接在哪台电脑，就让那台电脑作为控制端。', 12, secondary=True), 24, 96, 620, 20)
        self.role = self.segmented(['使用本机键鼠', '接受另一台电脑控制'],
                                  0 if s.role == 'controller' else 1, self.change_role)
        place(card, self.role, 22, 130, 628, 34)
        card = self.card(page, 208, 330, '连接你的另一台电脑')
        self.devices = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(A.NSMakeRect(0, 0, 10, 10), False)
        self.bind(self.devices, self.select_peer)
        place(card, self.devices, 22, 57, 628, 34)
        self.bluetooth_hint = label('先在此 Mac 开启蓝牙等待，再到 Windows 选择此 Mac 并连接。\n无需在系统设置中配对，也无需填写 IP 地址。', 12, secondary=True)
        self.bluetooth_hint.setLineBreakMode_(A.NSLineBreakByWordWrapping)
        self.bluetooth_hint.setMaximumNumberOfLines_(2)
        place(card, self.bluetooth_hint, 24, 52, 620, 44)
        self.discovery_label = place(card, label('', 11, secondary=True), 24, 98, 620, 20)
        place(card, label('配对密码', 12, A.NSFontWeightMedium), 24, 136, 610, 20)
        self.password = A.NSSecureTextField.alloc().init()
        self.password.setPlaceholderString_('两台电脑使用相同密码，4 位数字即可')
        self.password.setFont_(A.NSFont.systemFontOfSize_(13))
        place(card, self.password, 22, 163, 628, 30)
        place(card, label('密码仅在当前运行期间保留，用于断线后自动重连。', 11, secondary=True), 24, 202, 615, 20)
        self.manual = A.NSButton.checkboxWithTitle_target_action_('手动填写 IP 地址', self, 'action:')
        self.manual.setState_(s.manual)
        self.bind(self.manual, self.change_manual)
        place(card, self.manual, 20, 239, 210, 24, 0)
        self.help_button = place(card, self.button('连接帮助', self.help), 540, 233, 112, 32,
                                  A.NSViewMinXMargin)
        self.host = A.NSTextField.alloc().init()
        self.host.setPlaceholderString_('局域网 IP，例如 192.168.1.10')
        self.host.setStringValue_(s.host)
        place(card, self.host, 22, 281, 470, 28)
        self.port = A.NSTextField.alloc().init()
        self.port.setStringValue_(str(s.port))
        self.port.setPlaceholderString_('端口')
        place(card, self.port, 510, 281, 140, 28, A.NSViewMinXMargin)
        self.bluetooth_limits = label('蓝牙：键鼠与 8 KB 内文本剪贴板。\n文件和长文本请使用局域网。', 11, secondary=True)
        self.bluetooth_limits.setLineBreakMode_(A.NSLineBreakByWordWrapping)
        self.bluetooth_limits.setMaximumNumberOfLines_(2)
        place(card, self.bluetooth_limits, 24, 239, 475, 46)

    @objc.python_method
    def build_layout(self):
        s = self.session.settings
        page = self.pages[1]
        card = self.card(page, 4, 286, '屏幕排列', '选择另一台电脑所在的方向，鼠标移到对应边缘即可跨屏。')
        self.layout_labels = []
        for x in (128, 368):
            box = A.NSBox.alloc().init()
            box.setBoxType_(A.NSBoxCustom)
            box.setTitlePosition_(A.NSNoTitle)
            box.setFillColor_(A.NSColor.windowBackgroundColor())
            box.setBorderColor_(A.NSColor.controlAccentColor())
            box.setBorderWidth_(1)
            box.setCornerRadius_(10)
            place(card, box, x, 95, 176, 102, 0)
            self.layout_labels.append(place(card, label('', 18, A.NSFontWeightMedium), x + 16, 119, 144, 30, 0))
            place(card, label('显示器', 11, secondary=True), x + 16, 155, 144, 20, 0)
        place(card, label('↔', 24, secondary=True), 321, 126, 34, 32, 0)
        self.side = self.segmented(['另一台在左侧', '另一台在右侧'],
                                  1 if s.side == 'right' else 0,
                                  lambda i: self.session.configure(side='right' if i else 'left'))
        place(card, self.side, 110, 226, 455, 34)
        card = self.card(page, 306, 174, '鼠标速度', '按屏幕 DPI 换算；连接时也可以即时调整。')
        self.speed_value = place(card, label(f'{s.speed:.0%}', 15, A.NSFontWeightSemibold),
                                 546, 20, 100, 24, A.NSViewMinXMargin)
        self.speed = A.NSSlider.sliderWithValue_minValue_maxValue_target_action_(s.speed, .25, 4., self, 'action:')
        self.bind(self.speed, lambda: self.session.set_speed(self.speed.doubleValue()))
        self.speed.setContinuous_(True)
        place(card, self.speed, 22, 92, 628, 28)
        place(card, label('25% · 更精细', 11, secondary=True), 24, 131, 220, 20)
        right = place(card, label('400% · 更快速', 11, secondary=True), 442, 131, 208, 20, A.NSViewMinXMargin)
        right.setAlignment_(A.NSTextAlignmentRight)

    @objc.python_method
    def build_sharing(self):
        page = self.pages[2]
        page.setFrameSize_(A.NSMakeSize(690, 610))
        self.switches = {}
        rows = [('clipboard', '共享剪贴板', '在两台电脑间复制和粘贴文本。'),
                ('files', '允许接收文件', '接收的文件保存到“下载 / Share Monkey”。'),
                ('mapping', '快捷键适配', '在 Mac 与 Windows 间映射 Command 和 Control。'),
                ('auto_reconnect', '断线自动恢复', '暂时断线后自动重新连接，点击“断开”可停止。')]
        for i, (key, title, detail) in enumerate(rows):
            card = self.card(page, 4 + i * 112, 96, title, detail)
            switch = A.NSSwitch.alloc().init()
            switch.setState_(getattr(self.session.settings, key))
            switch.setAccessibilityLabel_(title)
            self.bind(switch, lambda k=key, c=switch: self.session.configure(**{k: bool(c.state())}))
            place(card, switch, 594, 26, 54, 28, A.NSViewMinXMargin)
            self.switches[key] = switch
        place(page, label('共享设置在下次连接时生效。', 12, secondary=True), 28, 468, 610, 22)
        self.file_button = place(page, self.button('选择文件并发送…', self.send_file), 24, 510, 180, 36, 0)
        self.file_notice = place(page, label('单个文件最大 100 MB，对方需允许接收。', 11, secondary=True),
                                 28, 559, 610, 22)

    @objc.python_method
    def make_status_item(self):
        self.status_item = A.NSStatusBar.systemStatusBar().statusItemWithLength_(A.NSVariableStatusItemLength)
        image = A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            'cursorarrow.motionlines', 'Share Monkey')
        image.setTemplate_(True)
        image.setSize_(A.NSMakeSize(19, 19))
        self.status_item.button().setImage_(image)
        self.status_item.button().setToolTip_('Share Monkey · 键鼠共享')
        menu = A.NSMenu.alloc().initWithTitle_('Share Monkey')
        heading = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('Share Monkey', None, '')
        menu.setAutoenablesItems_(False)
        heading.setEnabled_(False)
        menu.addItem_(heading)
        self.tray_status = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('准备就绪', None, '')
        self.tray_status.setEnabled_(False)
        menu.addItem_(self.tray_status)
        menu.addItem_(A.NSMenuItem.separatorItem())
        self.menu_item(menu, '打开主窗口', self.show_window)
        self.tray_disconnect = self.menu_item(menu, '断开连接', self.session.disconnect)
        menu.addItem_(A.NSMenuItem.separatorItem())
        self.menu_item(menu, '退出 Share Monkey', self.quit, 'q')
        self.status_item.setMenu_(menu)

    @objc.python_method
    def navigate(self, page):
        self.page = page
        titles = ['连接与设备', '键鼠与屏幕', '共享设置']
        details = [self.connection_subtitle(),
                   '让指针的方向和速度，都符合你的习惯。', '让电脑之间的工作自然衔接。']
        self.page_title.setStringValue_(titles[page])
        self.page_subtitle.setStringValue_(details[page])
        self.scroll.setDocumentView_(self.pages[page])
        for i, button in enumerate(self.nav):
            self.nav_backgrounds[i].setHidden_(i != page)
            button.setContentTintColor_(A.NSColor.controlAccentColor() if i == page else A.NSColor.labelColor())

    @objc.python_method
    def connection_subtitle(self):
        return ('无需网络。先在此 Mac 开启蓝牙等待，再从 Windows 连接。'
                if self.session.settings.transport == 'bluetooth' else
                '同一网络，自动发现。Mac 与 Windows 从这里连接。')

    @objc.python_method
    def refresh(self):
        if not hasattr(self, 'connect_button') or self.closed:
            return
        s = self.session.settings
        bluetooth = s.transport == 'bluetooth'
        running, connected = self.session.running, self.session.connected
        self.state_label.setStringValue_(self.session.status)
        self.state_label.setToolTip_(self.session.status)
        self.connect_button.setTitle_('开启蓝牙等待' if bluetooth else '连接' if s.role == 'controller' else '开始等待')
        self.connect_button.setEnabled_(not running)
        self.disconnect_button.setEnabled_(running)
        self.tray_disconnect.setEnabled_(running)
        self.tray_status.setTitle_(self.session.status)
        self.status_item.button().setToolTip_('Share Monkey · ' + self.session.status)
        self.sidebar_status.setStringValue_('●  已连接' if connected else '●  正在重连' if self.session.reconnecting else '●  等待连接' if running else '●  本机可用')
        self.sidebar_status.setTextColor_(A.NSColor.systemGreenColor() if connected else A.NSColor.secondaryLabelColor())
        for control in [self.transport, self.role, self.manual, self.devices, self.host, self.port, self.side, *self.switches.values()]:
            control.setEnabled_(not running)
        self.transport.setSelectedSegment_(1 if bluetooth else 0)
        self.role.setSelectedSegment_(0 if s.role == 'controller' else 1)
        self.manual.setState_(s.manual)
        self.side.setSelectedSegment_(1 if s.side == 'right' else 0)
        for key, switch in self.switches.items():
            # Display the effective Bluetooth setting without overwriting the
            # user's saved LAN preference; restore it when switching back.
            switch.setState_(bool(getattr(s, key)) and not (bluetooth and key == 'files'))
        self.switches['files'].setEnabled_(not running and not bluetooth)
        self.file_button.setEnabled_(connected and not bluetooth)
        self.file_notice.setStringValue_('蓝牙模式不传输文件，请切换为局域网。' if bluetooth else
                                         '单个文件最大 100 MB，对方需允许接收。')
        if self.page == 0:
            self.page_subtitle.setStringValue_(self.connection_subtitle())
        self.manual.setHidden_(bluetooth)
        self.host.setHidden_(bluetooth or not s.manual)
        self.port.setHidden_(bluetooth or not s.manual)
        self.devices.setHidden_(bluetooth or s.manual or s.role == 'receiver')
        self.bluetooth_hint.setHidden_(not bluetooth)
        self.bluetooth_limits.setHidden_(not bluetooth)
        self.help_button.setTitle_('蓝牙配置' if bluetooth else '连接帮助')
        self.discovery_label.setStringValue_(('手动连接使用同一网络中的 IP 地址。' if s.manual and not bluetooth else self.session.discovery_status))
        labels = list(self.session.peers)
        desired = labels or ['尚未发现设备']
        if list(self.devices.itemTitles()) != desired:
            self.devices.removeAllItems()
            self.devices.addItemsWithTitles_(desired)
        if self.session.selected:
            self.devices.selectItemWithTitle_(self.session.selected)
        names = ['Mac · 本机', self.client.peer] if s.side == 'right' else [self.client.peer, 'Mac · 本机']
        for view, text in zip(self.layout_labels, names):
            view.setStringValue_(text)
        self.speed_value.setStringValue_(f'{s.speed:.0%}')
        if running and self.activity is None:
            self.activity = F.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                F.NSActivityUserInitiatedAllowingIdleSystemSleep, 'Share Monkey 键鼠共享连接')
        elif not running and self.activity is not None:
            F.NSProcessInfo.processInfo().endActivity_(self.activity)
            self.activity = None

    @objc.python_method
    def change_role(self, index):
        self.session.configure(role='controller' if index == 0 else 'receiver')

    @objc.python_method
    def change_manual(self):
        self.session.configure(manual=bool(self.manual.state()))

    @objc.python_method
    def select_peer(self):
        self.session.selected = str(self.devices.titleOfSelectedItem())

    @objc.python_method
    def connect(self):
        if self.session.settings.manual and self.session.settings.transport == 'lan':
            self.session.settings.host = str(self.host.stringValue())
            try:
                self.session.settings.port = int(self.port.stringValue())
            except ValueError:
                raise ValueError('请输入 1024–65535 之间的端口号。') from None
        self.session.connect(str(self.password.stringValue()))

    @objc.python_method
    def send_file(self):
        panel = A.NSOpenPanel.openPanel()
        panel.setTitle_('选择发送的文件（最多 100 MB）')
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        if panel.runModal() == A.NSModalResponseOK:
            self.session.send_file(str(panel.URL().path()))

    @objc.python_method
    def alert(self, text, title='Share Monkey'):
        alert = A.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.addButtonWithTitle_('好')
        alert.runModal()

    @objc.python_method
    def help(self):
        if self.session.settings.transport == 'bluetooth':
            self.bluetooth_help_alert().runModal()
        else:
            self.alert(self.client.help, '连接帮助')

    @objc.python_method
    def bluetooth_help_alert(self):
        """Keep the setup guide readable on small displays and at large fonts."""
        alert = A.NSAlert.alloc().init()
        alert.setMessageText_('蓝牙配置')
        alert.setInformativeText_('按照下方步骤连接。说明可以滚动，也可以选择文字复制。')
        alert.addButtonWithTitle_('完成')
        screen = self.window.screen() or A.NSScreen.mainScreen()
        available = screen.visibleFrame().size if screen is not None else A.NSMakeSize(1024, 768)
        width = min(600, max(320, available.width - 180))
        height = min(420, max(220, available.height - 300))
        scroll = A.NSScrollView.alloc().initWithFrame_(A.NSMakeRect(0, 0, width, height))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(A.NSBezelBorder)
        size = scroll.contentSize()
        text = A.NSTextView.alloc().initWithFrame_(A.NSMakeRect(0, 0, size.width, size.height))
        text.setEditable_(False)
        text.setSelectable_(True)
        text.setRichText_(False)
        text.setFont_(A.NSFont.systemFontOfSize_(13))
        text.setTextColor_(A.NSColor.labelColor())
        text.setBackgroundColor_(A.NSColor.textBackgroundColor())
        text.setTextContainerInset_(A.NSMakeSize(14, 14))
        text.setMinSize_(A.NSMakeSize(0, size.height))
        text.setMaxSize_(A.NSMakeSize(1e7, 1e7))
        text.setHorizontallyResizable_(False)
        text.setVerticallyResizable_(True)
        text.setAutoresizingMask_(A.NSViewWidthSizable)
        text.textContainer().setContainerSize_(A.NSMakeSize(size.width, 1e7))
        text.textContainer().setWidthTracksTextView_(True)
        text.setString_(setup_help('darwin'))
        text.setAccessibilityLabel_('蓝牙直连配置说明')
        scroll.setDocumentView_(text)
        text.layoutManager().ensureLayoutForTextContainer_(text.textContainer())
        text.sizeToFit()
        alert.setAccessoryView_(scroll)
        return alert

    @objc.python_method
    def about(self):
        self.alert(f'版本 {__version__}\n一套键鼠，流畅穿梭。\n\n关闭窗口后，Share Monkey 仍会在菜单栏运行。', 'Share Monkey')

    @objc.python_method
    def show_window(self):
        if self.closed:
            return
        self.window_open_requested = True
        token = self.capture_ui.current
        if token is not None:
            # Return first: cancelling the token wakes the network writer.
            # It must see remote=False rather than treat this as capture loss.
            if self.session.engine is not None:
                self.session.engine.return_local()
            token.cancel()
            return
        self._flush_window_request()

    @objc.python_method
    def _flush_window_request(self):
        if self.closed or not self.window_open_requested or self.capture_ui.current is not None:
            return
        engine = self.session.engine
        if engine is not None and isinstance(engine.pending_return, tuple):
            return
        self.window_open_requested = False
        self.window.makeKeyAndOrderFront_(None)
        self.app.activateIgnoringOtherApps_(True)

    @objc.python_method
    def hide_window(self):
        self.window_open_requested = False
        self.window.orderOut_(None)

    def windowShouldClose_(self, window):
        self.hide_window()
        return False

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self.show_window()
        return True

    def applicationShouldTerminate_(self, app):
        self.cleanup()
        return A.NSTerminateNow

    def tick_(self, timer):
        if self.closed:
            return
        self.session.poll()
        if self.guard and self.guard.consume_show_request():
            self.show_window()
        self._flush_window_request()

    @objc.python_method
    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self.timer.invalidate()
        self.session.close()
        self.capture_ui.close()
        if self.activity is not None:
            F.NSProcessInfo.processInfo().endActivity_(self.activity)
            self.activity = None
        A.NSStatusBar.systemStatusBar().removeStatusItem_(self.status_item)

    @objc.python_method
    def quit(self):
        self.cleanup()
        self.window.orderOut_(None)
        self.app.stop_(None)
        event = A.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            A.NSEventTypeApplicationDefined, A.NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
        self.app.postEvent_atStart_(event, True)

    @objc.python_method
    def snapshot(self, path):
        view = self.window.contentView()
        bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
        bitmap.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)


def main(client=MAC):
    smoke = '--smoke-test' in sys.argv or '--input-smoke-test' in sys.argv
    guard = None
    if not smoke:
        guard = InstanceLock()
        try:
            guard.acquire()
        except AlreadyRunning:
            guard.request_show()
            return
    app = Desktop.alloc().init().setup(client, smoke, guard)
    failures = []
    try:
        if '--input-smoke-test' in sys.argv:
            from .display import primary_screen
            from .engine import Engine
            from .input import Input
            engine = Engine(True, primary_screen(), 'right', 'diagnostic-not-used',
                            '127.0.0.1', 24809, lambda value: None, clipboard=False)
            app.session.engine = engine
            engine.input = Input(engine)
            engine.input.start()
            def check_input():
                if not all(listener.is_alive() for listener in engine.input.listeners):
                    failures.append('键鼠监听线程意外退出')
                app.quit()
            F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.5, False, lambda timer: check_input())
        elif smoke:
            F.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(.6, False, lambda timer: app.quit())
        app.show_window()
        app.app.run()
    finally:
        app.cleanup()
        if guard:
            guard.close()
    if failures:
        raise RuntimeError('; '.join(failures))


if __name__ == '__main__':
    main()
