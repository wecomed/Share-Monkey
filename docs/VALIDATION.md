# 验证范围

当前源码与安装包版本：macOS / Windows **0.5.8**。本项目仍处于开发阶段；局部测试通过不代表完整双机发布验收。

## 已完成

| 检查 | 范围 |
| --- | --- |
| 单元与集成回归 | 540 项通过，包含键鼠事件引用、断线重连、配对密码、设备标识和协议检查 |
| 加密会话恢复 | 使用真实 Session、Engine、Channel、AES-GCM 和 BleStream；无线与系统输入为替身，两方向共 20 次会话、18 次自动重连 |
| macOS 原生监听 | 20 轮生产键鼠监听启停，含 19 次自动恢复；传输故障在内存模拟，无实际 BLE 对端；线程均已退出 |
| macOS 最终安装包 | 连续 3 次原生监听自检成功退出；签名、版本、归档源码一致性检查通过 |
| Windows 构建产物 | 272 个 x64 载荷、80 个运行依赖、2,704 个文件清单哈希，以及最终安装器解包校验通过 |
| Windows 电源请求 | API 生命周期与结构布局使用注入接口检查，没有在 Windows 上实际执行 |

0.5.7 的事件引用泄漏修复另外完成过 1,000,000 次原生回调压力检查，Python 和原生事件引用数保持不变；后续回归继续覆盖这条路径。

## 尚未完成

- 当前构建在真实 Windows 上的安装、界面、关屏、锁屏、睡眠及唤醒检查。
- 当前构建真实双机 BLE 混合输入的连续耐久验收。
- 将模拟传输、本机原生检查和实际双机体验统一完成的完整发布门禁。

因此安装包应按预发布构建使用，不能将静态包检查描述为 Windows 真机通过。

## 运行检查

在项目目录建立虚拟环境并安装对应平台依赖后，先运行：

```sh
python -m unittest discover -s tests -v
python tools/verify_release.py --profile quick --output-dir validation/current
```

部分回归需要绑定本机 UDP 端口；原生平台测试会按系统选择或跳过。发布验收不能把跳过项算作已通过。

Mac 原生检查需显式启用，使用已有系统权限：

```sh
python tools/probe_mac_memory.py --native --count 1000000 --output validation/memory/report.json
python tools/probe_mac_reconnect.py --native --cycles 20 --output validation/reconnect/report.json
```

上述两个探针不会向系统注入按键或鼠标移动、读取系统剪贴板，或连接另一台电脑。窗口捕获、焦点和完整发布检查另见 [验收工具说明](../validation/README.md)。

输出文件留在本机。公开仓库不包含运行日志、真实设备快照、进程采样、崩溃报告或配对密码。
