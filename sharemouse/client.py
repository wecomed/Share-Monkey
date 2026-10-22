from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class Client:
    platform: str
    name: str
    peer: str
    default_role: str
    help: str


MAC = Client('darwin', 'Mac', 'Windows', 'controller',
             'Mac：在系统设置 → 隐私与安全性中，授权本客户端“辅助功能”和“输入监控”，然后重启客户端。\n\n'
             'Windows：选择“接受另一台电脑控制”，设置相同配对密码并开始等待。Mac 会自动发现它，选择设备后连接，无需输入 IP。\n\n'
             '未发现设备时，检查系统本地网络权限及防火墙是否允许 UDP 24810；也可勾选手动填写 IP 地址。\n\n'
             '键鼠接在 Windows 时，两端交换控制端 / 接收端角色。F12 返回连接键鼠的电脑。')
WINDOWS = Client('win32', 'Windows', 'Mac', 'receiver',
                 'Windows：选择“接受另一台电脑控制”，设置配对密码并开始等待。防火墙提示时允许专用网络访问；默认端口 TCP 24809。\n\n'
                 'Mac：选择“使用本机键鼠”，自动发现这台电脑，选择设备并输入相同密码后连接，无需输入 IP。\n\n'
                 '自动发现使用 UDP 24810；防火墙需允许客户端在专用网络上访问。找不到设备时可以勾选手动填写 IP 地址。\n\n'
                 '键鼠接在 Windows 时，两端交换控制端 / 接收端角色。此客户端不能控制 UAC 安全桌面。')


def current_client():
    return WINDOWS if sys.platform == 'win32' else MAC


def launch(client):
    if sys.platform != client.platform:
        raise SystemExit(f'请在 {client.name} 系统上运行此客户端')
    if client.platform == 'win32' and '--cursor-guard' in sys.argv:
        # Frozen portable builds reuse their executable for the private cursor
        # recovery worker. Dispatch before importing Qt or the instance lock.
        from .cursor_guard import main as cursor_guard_main
        cursor_guard_main(sys.argv[sys.argv.index('--cursor-guard') + 1:])
        return
    if client.platform == 'darwin':
        from .native_mac import main
    else:
        from .native_windows import main
    main(client)
