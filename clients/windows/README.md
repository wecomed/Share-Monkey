# Share Monkey · Windows

版本 **0.5.8**，请搭配 Mac **0.5.8** 使用 4 位密码和自动重连。输入协议 3、蓝牙传输协议 3。保留快速蓝牙连接参数、处理确认、物理像素坐标和通知区常驻；新增断线自动恢复与关屏保活请求。

## 使用 EXE 安装

1. 退出旧客户端；已有通知区图标时，请从图标菜单选择“退出 Share Monkey”。
2. 双击 `Share-Monkey-0.5.8-Windows-Setup.exe` 完成安装。需要 64 位 Windows，目标系统为 Windows 11。
3. 从桌面或开始菜单打开 **Share Monkey**。运行环境已内置，无需另装 Python。
4. 局域网模式默认选择接收端；输入配对密码，4 位数字即可，点击“开始等待”。Mac 自动发现后输入相同密码连接。
5. 防火墙提示时允许专用网络访问。连接 TCP 24809，发现 UDP 24810；安装器不会修改防火墙规则。

默认安装目录 `%LOCALAPPDATA%\Programs\Share Monkey`，仅安装到当前用户，不要求管理员权限，不添加开机启动。可在系统“已安装的应用”中卸载。安装程序尚未使用发布者证书签名。

## 蓝牙直连

1. 开启两端系统蓝牙；Windows 适配器和驱动要支持 BLE 中央设备模式。无需同一 Wi-Fi、IP 或 COM 端口。
2. 两端在客户端选“蓝牙直连”。键鼠接在哪台，就在哪台选“使用本机键鼠”，另一端选“接受另一台电脑控制”。填写相同的应用密码，4 位数字即可。连接前，在键鼠端的“键鼠与屏幕”设好左右位置。
3. **先让 Mac 点击“开启蓝牙等待”并允许蓝牙权限**，然后 Windows 选择发现的 SM Monkey · Mac · 蓝牙（设备地址），点击“连接 Mac”。无需提前在系统“添加蓝牙设备”配对，不要添加成蓝牙键盘或鼠标。
4. 两端显示“蓝牙已连接”后，移到设定边缘跨屏；F12 返回本机。无论控制方向，都是 Mac 等待、Windows 连接。更改左右位置需先断开。
5. 默认开启断线自动恢复，Mac 自动重新等待，Windows 自动连接；手动“断开”或退出会停止恢复。不能发现时检查两端蓝牙、Mac 隐私权限、Windows 驱动及底部状态；详细排查点“蓝牙配置”。

蓝牙只传键鼠、滚轮和最多8192个 UTF-8 字节的纯文本剪贴板。文件和大段文本需先断开切回局域网；文件接收开关在蓝牙模式不可用。无需为蓝牙修改防火墙或以管理员身份运行。

## 常驻与界面

关闭窗口后继续在任务栏通知区运行，图标可能位于 Windows 的隐藏图标列表中，可按系统设置显示。点击图标恢复窗口，右键菜单提供连接状态、断开和退出。通知区暂不可用时保留最小化窗口。重新打开快捷方式会唤回已有窗口。

界面使用 Qt Widgets，按 Windows 11 的圆角、间距、Fluent 开关和颜色设计，支持深浅色和高 DPI；使用 Segoe UI Variable Text / Segoe UI，中文回退 Microsoft YaHei UI。它不是 WinUI 3 程序。“关屏保持共享”默认开启，允许屏幕关闭，共享期间请求系统继续运行，不修改电源计划。真正睡眠、合盖和系统电池策略仍可能暂停通信，恢复后自动重连；强制结束、注销与重启不在常驻保护范围内。

“键鼠与屏幕”支持左右布局和 25%–400% 实时速度，Windows 默认 100%，自动补偿 DPI。其他设置需先断开再更改。共享偏好保存在 `%LOCALAPPDATA%\Share Monkey\settings.json`，不保存密码。日志在同目录的 `logs/client.log`，文件接收在 `Downloads/Share Monkey`。

文件发送通过“共享设置”页面完成，对方需预先允许接收，最大 100 MB。暂不支持桌面拖拽、多显示器、登录界面或 UAC 安全桌面。F12 返回控制端。

## 源码与构建

源码 ZIP 才需要先安装 Python 3.11+、双击 `install.bat`，再运行 `run-windows.bat`；这不是 EXE 安装包的使用步骤。开发源码可执行：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.venv\Scripts\python.exe tools\build_windows_installer.py --output-dir dist/v0.5.8
```

构建安装器需 NSIS。本项目也支持在 Mac 上组合官方 Windows 运行环境、Windows wheels 和 NSIS 生成 EXE，带文件清单与 SHA-256 校验。当前构建尚未在真实 Windows 中安装运行或完成关屏/唤醒及双机耐久验收；二进制检查与模拟测试不能替代该验证。详见 [验证说明](../../docs/VALIDATION.md)。
