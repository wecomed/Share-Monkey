"""Run explicitly in a GUI session; never installs input hooks."""
import tkinter as tk
from unittest.mock import Mock
from sharemouse.__main__ import App
from sharemouse.client import MAC, WINDOWS
from sharemouse.discovery import Peer

for profile in (MAC, WINDOWS):
    root = tk.Tk()
    app = App(root, profile, discovery_enabled=False)
    root.update()
    assert root.winfo_exists()
    assert app.role.get() == profile.default_role
    assert profile.name in root.title()
    assert app.mouse_speed.get() == (1.5 if profile == MAC else 1.0)
    app.engine = Mock()
    app.update_mouse_speed('2.0')
    app.engine.set_mouse_speed.assert_called_once_with(2.0)
    assert app.mouse_speed_label.get() == '200%'
    app.engine = None
    app.role.set('controller')
    app.update_role()
    assert not app.host_entry.winfo_ismapped()
    peer = Peer('192.168.1.5', 24809, 'Office PC', 'Windows')
    app.update_peers([peer])
    assert app.device.get() == peer.label
    app.update_peers([])
    assert app.device.get() == ''
    app.manual.set(True)
    app.update_connection_mode()
    root.update()
    assert app.host_entry.winfo_ismapped()
    app.role.set('receiver')
    app.update_role()
    assert app.host.get() == '0.0.0.0'
    app.side.set('left')
    app.draw()
    root.update()
    root.geometry('750x560')
    root.update()
    assert app.connect_button.winfo_rooty() >= root.winfo_rooty()
    assert (app.connect_button.winfo_rooty() + app.connect_button.winfo_height()
            <= root.winfo_rooty() + root.winfo_height())
    assert app.settings_viewport.yview()[1] < 1
    app.settings_viewport.yview_moveto(1)
    root.update()
    assert app.settings_viewport.yview()[1] == 1
    app.close()
print('Desktop UI smoke test passed')
