import queue
import socket
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .engine import Engine
from .runtime import configure_tk
from .client import current_client
from .discovery import Browser
from .display import primary_screen, enable_dpi_awareness

configure_tk()


class App:
    def __init__(self, root, client=None, discovery_enabled=True):
        self.root, self.engine = root, None
        self.client = client or current_client()
        self.events = queue.Queue()
        self.discovery_enabled = discovery_enabled
        self.browser = None
        self.peers = {}
        self.discovery_events = queue.Queue()
        self.discovery_generation = 0
        from .ui import build
        build(self)
        self.update_role()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def draw(self):
        c = self.canvas
        c.delete('all')
        labels = [self.client.name, self.client.peer] if self.side.get() == 'right' else [self.client.peer, self.client.name]
        width = max(500, c.winfo_width())
        accent = self.palette['accent']
        for x, label in zip((width / 2 - 185, width / 2 + 45), labels):
            local = label == self.client.name
            c.create_rectangle(x, 10, x + 140, 77, fill='#f7f9fc' if local else '#ffffff',
                               outline=accent if local else '#b8bbc1', width=2)
            c.create_line(x + 70, 77, x + 70, 90, fill='#b8bbc1', width=2)
            c.create_line(x + 48, 90, x + 92, 90, fill='#b8bbc1', width=2)
            c.create_text(x + 70, 36, text=label, fill=self.palette['ink'], font=('', 13, 'bold'))
            c.create_text(x + 70, 58, text='本机' if local else '另一台电脑', fill=self.palette['muted'], font=('', 10))
        c.create_text(width / 2, 43, text='↔', fill=accent, font=('', 22))

    def update_role(self):
        controller = self.role.get() == 'controller'
        if self.engine:
            self.disconnect()
        self.discovery_generation += 1
        generation = self.discovery_generation
        if self.browser:
            self.browser.stop()
            self.browser = None
        self.peers = {}
        self.device.set('')
        self.devices.configure(values=())
        self.host.set('' if controller else '0.0.0.0')
        self.connect_button.configure(text='连接' if controller else '开始等待')
        self.draw()
        self.update_connection_mode()
        if controller and self.discovery_enabled:
            self.browser = Browser(lambda peers: self.discovery_events.put((generation, peers)),
                                   lambda error: self.discovery_events.put((generation, error))).start()
        if not controller:
            try:
                addresses = socket.gethostbyname_ex(socket.gethostname())[2]
                ips = ', '.join(ip for ip in addresses if not ip.startswith('127.'))
                self.status.set('本机地址：' + (ips or '请在系统网络设置中查看局域网 IP'))
            except OSError:
                self.status.set('请在系统网络设置中查看本机局域网 IP')

    def update_connection_mode(self):
        controller = self.role.get() == 'controller'
        self.host_entry.grid_remove()
        self.devices.grid_remove()
        self.port_label.grid_remove()
        self.port_entry.grid_remove()
        if self.manual.get():
            self.host_label.configure(text='对方 IP 地址' if controller else '监听地址')
            self.host_entry.grid()
            self.port_label.grid()
            self.port_entry.grid()
            self.discovery_status.set('可手动填写局域网地址与端口')
        elif controller:
            self.host_label.configure(text='附近的电脑')
            self.devices.grid()
            self.discovery_status.set('正在搜索…请先在另一台电脑点击“开始等待”')
        else:
            self.host_label.configure(text='局域网连接')
            self.discovery_status.set('点击“开始等待”后，对方即可自动发现这台电脑')

    def update_peers(self, peers):
        self.peers = {peer.label: peer for peer in sorted(peers, key=lambda peer: peer.label)}
        self.devices.configure(values=tuple(self.peers))
        if self.device.get() not in self.peers:
            self.device.set(next(iter(self.peers)) if len(self.peers) == 1 else '')
        if not self.manual.get():
            self.discovery_status.set(f'发现 {len(peers)} 台电脑，选择后点击连接' if peers else
                                      '正在搜索…请先在另一台电脑点击“开始等待”')

    def connect(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        try:
            if len(self.password.get()) < 4:
                raise ValueError('请输入至少 4 个字符的配对密码，4 位数字即可')
            controller = self.role.get() == 'controller'
            host, port = self.host.get().strip(), int(self.port.get())
            if not self.manual.get():
                if controller:
                    peer = self.peers.get(self.device.get())
                    if not peer:
                        raise ValueError('请先在另一台电脑点击“开始等待”，再选择发现的电脑')
                    host, port = peer.host, peer.port
                else:
                    host = '0.0.0.0'
            if not 1024 <= port <= 65535:
                raise ValueError('端口范围为 1024–65535')
            if not host:
                raise ValueError('请输入接收端的局域网 IP 地址')
            screen = primary_screen(self.root.winfo_id())
            self.engine = Engine(controller, screen, self.side.get(),
                                 self.password.get(), host, port,
                                 self.events.put, self.clipboard.get(), self.files.get(), self.mapping.get(),
                                 mouse_speed=self.mouse_speed.get())
            self.engine.start()
        except Exception as exc:
            if self.engine:
                self.engine.stop()
                self.engine = None
            messagebox.showerror('无法启动', str(exc))

    def update_mouse_speed(self, value):
        speed = float(value)
        self.mouse_speed_label.set(f'{speed:.0%}')
        if self.engine:
            self.engine.set_mouse_speed(speed)

    def disconnect(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.status.set('已断开 · 本机键鼠已恢复')

    def send_file(self):
        if not self.engine or not self.engine.connected:
            messagebox.showinfo('发送文件', '请先连接另一台电脑')
            return
        filename = filedialog.askopenfilename(title='选择发送的文件（最多 100 MB）')
        if filename:
            self.engine.send_file(filename)

    def poll(self):
        while True:
            try:
                generation, value = self.discovery_events.get_nowait()
                if generation == self.discovery_generation:
                    if isinstance(value, str):
                        self.discovery_status.set(value)
                    else:
                        self.update_peers(value)
            except queue.Empty:
                break
        while True:
            try:
                self.status.set(self.events.get_nowait())
            except queue.Empty:
                break
        self.root.after(100, self.poll)

    def close(self):
        if self.browser:
            self.browser.stop()
        if self.engine:
            self.engine.stop()
        self.root.destroy()


def main(client=None):
    enable_dpi_awareness()
    root = tk.Tk()
    smoke = '--smoke-test' in sys.argv or '--input-smoke-test' in sys.argv
    from .instance import InstanceLock, AlreadyRunning
    guard = None
    if not smoke:
        try:
            guard = InstanceLock().acquire()
        except AlreadyRunning as exc:
            root.withdraw()
            messagebox.showinfo('客户端已运行', str(exc))
            root.destroy()
            return
    app = App(root, client, discovery_enabled=not smoke)
    failures = []
    if '--input-smoke-test' in sys.argv:
        from .input import Input
        engine = Engine(True, primary_screen(),
                        'right', 'diagnostic-not-used', '127.0.0.1', 24809, app.events.put,
                        clipboard=False)
        app.engine = engine
        engine.input = Input(engine)
        try:
            # No network connection, no suppression, no input injection.
            engine.input.start()
        except Exception:
            app.close()
            raise
        def finish_input_check():
            if not all(listener.is_alive() for listener in engine.input.listeners):
                failures.append('键鼠监听线程意外退出')
            app.close()
        root.after(1500, finish_input_check)
    elif '--smoke-test' in sys.argv:
        # Exercise bundled native imports without installing input hooks.
        from .input import Input
        if sys.platform == 'darwin':
            from .mac_mouse import MouseController, MouseListener
            MouseController()
            MouseListener(on_delta=lambda *args: None)
        root.after(200, app.close)
    try:
        root.mainloop()
    finally:
        if app.engine:
            app.engine.stop()
        if guard:
            guard.close()
    if failures:
        raise RuntimeError('; '.join(failures))


if __name__ == '__main__':
    main()
