"""Platform-themed settings surfaces; controls keep their native ttk theme."""
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from . import __version__


def build(app):
    root, client = app.root, app.client
    mac = client.platform == 'darwin'
    if mac and sys.platform == 'darwin':
        from AppKit import NSApplication, NSAppearance, NSAppearanceNameAqua
        # Aqua otherwise inherits system dark mode while our canvas and label
        # palette remain light. Scope a consistent appearance to this app.
        NSApplication.sharedApplication().setAppearance_(
            NSAppearance.appearanceNamed_(NSAppearanceNameAqua))
        root.tk.call('::tk::unsupported::MacWindowStyle', 'appearance', root._w, 'aqua')
    surface = '#ececec' if mac else '#f3f3f3'
    ink, muted = ('#1d1d1f', '#6e6e73') if mac else ('#1b1b1b', '#606060')
    accent = '#007aff' if mac else '#0067c0'
    font = 'Helvetica Neue' if mac else 'Segoe UI'
    app.palette = {'surface': surface, 'ink': ink, 'muted': muted, 'accent': accent}
    root.title(f'Share Monkey v{__version__} · {client.name} 客户端')
    height = min(820 if mac else 840, max(560, root.winfo_screenheight() - 110))
    root.geometry(f'{800 if mac else 840}x{height}')
    root.minsize(750, 560)
    root.configure(background=surface)
    style = ttk.Style(root)
    native = 'aqua' if mac else 'vista'
    style.theme_use(native if native in style.theme_names() else 'clam')
    style.configure('TFrame', background=surface)
    style.configure('Card.TFrame', background='white')
    style.configure('TLabel', background=surface, foreground=ink, font=(font, 12))
    style.configure('Card.TLabel', background='white')
    style.configure('Title.TLabel', font=(font, 25, 'bold'))
    style.configure('Section.TLabel', background='white', font=(font, 13, 'bold'))
    style.configure('Muted.TLabel', foreground=muted, font=(font, 11))
    style.configure('CardMuted.TLabel', background='white', foreground=muted, font=(font, 11))
    style.configure('TButton', font=(font, 12), padding=(12, 6))
    style.configure('TRadiobutton', background='white', font=(font, 12))
    style.configure('TCheckbutton', background='white', font=(font, 12))
    style.configure('TEntry', font=(font, 12))
    style.configure('TCombobox', font=(font, 12), padding=4)
    if native not in style.theme_names():
        style.map('TRadiobutton', background=[('active', 'white')])
        style.map('TCheckbutton', background=[('active', 'white')])

    app.role = tk.StringVar(value=client.default_role)
    app.host, app.device, app.discovery_status = tk.StringVar(), tk.StringVar(), tk.StringVar()
    app.manual = tk.BooleanVar(value=False)
    app.port = tk.StringVar(value='24809')
    app.password = tk.StringVar()
    app.side = tk.StringVar(value='right')
    app.mouse_speed = tk.DoubleVar(value=1.5 if mac else 1.0)
    app.mouse_speed_label = tk.StringVar(value=f'{app.mouse_speed.get():.0%}')
    app.clipboard, app.files, app.mapping = tk.BooleanVar(value=True), tk.BooleanVar(value=False), tk.BooleanVar(value=True)
    app.status = tk.StringVar(value='尚未连接')

    body = ttk.Frame(root, padding=(30, 24))
    body.pack(fill='both', expand=True)
    header = ttk.Frame(body)
    header.pack(fill='x', pady=(0, 18))
    ttk.Label(header, text='键鼠共享', style='Title.TLabel').pack(side='left')
    ttk.Label(header, text=f'{client.name}  ·  v{__version__}', style='Muted.TLabel').pack(side='right', anchor='s', pady=7)
    ttk.Label(body, text='一套键鼠，在两台电脑之间自然切换。', style='Muted.TLabel').pack(anchor='w', pady=(0, 16))

    settings = ttk.Frame(body)
    settings.pack(fill='both', expand=True)
    viewport = tk.Canvas(settings, background=surface, highlightthickness=0)
    scrollbar = ttk.Scrollbar(settings, orient='vertical', command=viewport.yview)
    scrollbar.pack(side='right', fill='y')
    viewport.pack(side='left', fill='both', expand=True)
    content_area = ttk.Frame(viewport)
    content_id = viewport.create_window((0, 0), window=content_area, anchor='nw')
    content_area.bind('<Configure>', lambda event: viewport.configure(scrollregion=viewport.bbox('all')))
    viewport.bind('<Configure>', lambda event: viewport.itemconfigure(content_id, width=event.width))
    def update_scrollbar(first, last):
        scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            scrollbar.pack_forget()
        elif not scrollbar.winfo_manager():
            scrollbar.pack(side='right', fill='y', before=viewport)

    viewport.configure(yscrollcommand=update_scrollbar)

    def scroll(event):
        if content_area.winfo_reqheight() <= viewport.winfo_height():
            return
        if not str(event.widget).startswith(str(settings)):
            return
        delta = event.delta if sys.platform == 'darwin' else event.delta / 120
        viewport.yview_scroll(-int(delta), 'units')

    root.bind('<MouseWheel>', scroll, add='+')
    app.settings_viewport = viewport

    def card(title):
        border = tk.Frame(content_area, background='white', highlightbackground='#dedee3' if mac else '#dedede', highlightthickness=1)
        border.pack(fill='x', pady=(0, 12))
        content = ttk.Frame(border, style='Card.TFrame', padding=(18, 14))
        content.pack(fill='x')
        ttk.Label(content, text=title, style='Section.TLabel').pack(anchor='w', pady=(0, 10))
        return content

    connection = card('连接设备')
    roles = ttk.Frame(connection, style='Card.TFrame')
    roles.pack(fill='x', pady=(0, 10))
    ttk.Radiobutton(roles, text='使用本机键鼠', variable=app.role, value='controller', command=app.update_role).pack(side='left')
    ttk.Radiobutton(roles, text='接受另一台电脑控制', variable=app.role, value='receiver', command=app.update_role).pack(side='left', padx=24)
    form = ttk.Frame(connection, style='Card.TFrame')
    form.pack(fill='x')
    form.columnconfigure(1, weight=1)
    app.host_label = ttk.Label(form, text='附近的电脑', style='Card.TLabel')
    app.host_label.grid(row=0, column=0, sticky='w', pady=7)
    app.host_entry = ttk.Entry(form, textvariable=app.host)
    app.host_entry.grid(row=0, column=1, sticky='ew', padx=(20, 0))
    app.devices = ttk.Combobox(form, textvariable=app.device, state='readonly')
    app.devices.grid(row=0, column=1, sticky='ew', padx=(20, 0))
    app.port_label = ttk.Label(form, text='端口', style='Card.TLabel')
    app.port_label.grid(row=1, column=0, sticky='w', pady=7)
    app.port_entry = ttk.Entry(form, textvariable=app.port)
    app.port_entry.grid(row=1, column=1, sticky='ew', padx=(20, 0))
    ttk.Label(form, text='配对密码', style='Card.TLabel').grid(row=2, column=0, sticky='w', pady=7)
    ttk.Entry(form, textvariable=app.password, show='•').grid(row=2, column=1, sticky='ew', padx=(20, 0))
    ttk.Label(form, text='两端输入相同密码，4 位数字即可。', style='CardMuted.TLabel').grid(row=3, column=1, sticky='w', padx=(20, 0), pady=(3, 8))
    ttk.Checkbutton(form, text='手动填写 IP 地址', variable=app.manual, command=app.update_connection_mode).grid(row=4, column=1, sticky='w', padx=(20, 0))
    ttk.Label(form, textvariable=app.discovery_status, style='CardMuted.TLabel', wraplength=480).grid(row=5, column=1, sticky='w', padx=(20, 0), pady=(8, 0))

    arrangement = card('屏幕排列')
    row = ttk.Frame(arrangement, style='Card.TFrame')
    row.pack(fill='x')
    ttk.Label(row, text='另一台电脑在本机的', style='Card.TLabel').pack(side='left', padx=(0, 20))
    ttk.Radiobutton(row, text='左侧', variable=app.side, value='left', command=app.draw).pack(side='left')
    ttk.Radiobutton(row, text='右侧', variable=app.side, value='right', command=app.draw).pack(side='left', padx=18)
    app.canvas = tk.Canvas(arrangement, height=100, background='white', highlightthickness=0)
    app.canvas.pack(fill='x', pady=(8, 0))
    app.canvas.bind('<Configure>', lambda event: app.draw())

    speed_row = ttk.Frame(arrangement, style='Card.TFrame')
    speed_row.pack(fill='x', pady=(8, 0))
    ttk.Label(speed_row, text='远端鼠标速度', style='Card.TLabel').pack(side='left', padx=(0, 20))
    ttk.Scale(speed_row, from_=.25, to=4, variable=app.mouse_speed,
              command=app.update_mouse_speed).pack(side='left', fill='x', expand=True)
    ttk.Label(speed_row, textvariable=app.mouse_speed_label, width=6,
              style='Card.TLabel').pack(side='left', padx=(12, 0))
    ttk.Label(arrangement, text='自动匹配显示缩放；速度可在连接后直接调整。',
              style='CardMuted.TLabel').pack(anchor='w', pady=(5, 0))

    sharing = card('共享选项')
    options = ttk.Frame(sharing, style='Card.TFrame')
    options.pack(fill='x')
    ttk.Checkbutton(options, text='文本剪贴板', variable=app.clipboard).pack(side='left')
    ttk.Checkbutton(options, text='允许接收文件', variable=app.files).pack(side='left', padx=22)
    ttk.Checkbutton(options, text='Command / Ctrl 映射', variable=app.mapping).pack(side='left')
    ttk.Label(sharing, text='文件保存到「下载 / Share Monkey」，每个文件最多 100 MB。', style='CardMuted.TLabel').pack(anchor='w', pady=(9, 0))

    footer = ttk.Frame(body)
    footer.pack(fill='x', pady=(4, 10))
    app.connect_button = ttk.Button(footer, text='连接', command=app.connect)
    app.connect_button.pack(side='left')
    ttk.Button(footer, text='断开', command=app.disconnect).pack(side='left', padx=8)
    ttk.Button(footer, text='发送文件…', command=app.send_file).pack(side='left')
    ttk.Button(footer, text='帮助', command=lambda: messagebox.showinfo(f'{client.name} 客户端帮助', client.help)).pack(side='right')
    status_label = ttk.Label(body, textvariable=app.status, wraplength=680)
    status_label.pack(anchor='w', pady=(0, 5))
    body.bind('<Configure>', lambda event: status_label.configure(wraplength=max(300, event.width - 60)))
    ttk.Label(body, text='F12 返回本机  ·  返回后可直接再次跨屏', style='Muted.TLabel').pack(anchor='w')
