# Share Monkey 稳定性验收

当前 Mac / Windows 构建均为 **0.5.8**。自动重连、Windows 关屏保活请求与 4 位密码的专项结果在 `v058/REPORT.md`；Windows 真机关屏/唤醒及真实双机耐久尚未验收。内存修复证据保存在 `memory-20261021/`。保留历史报告，完整发布门槛未放行。

目标是让开发方完成重复验证，用户只做最后的使用体验确认。不同测试层分别报告，不能用模拟结果替代系统原生输入，也不能用本机原生测试替代真实双机蓝牙。

Mac 事件引用和原生分配压力检查（不注册监听、不注入输入、不读实际键值）：

```sh
.venv/bin/python tools/probe_mac_memory.py --native --count 1000000 --output validation/memory-20261021/event-callback-fixed.json
```

该检查验证生产事件回调的传入、拦截和替换事件所有权；全套单元测试也包含此泄漏的引用数回归。持续真实双机蓝牙测试仍需单独验收。

## 统一入口

在项目根目录运行：

```sh
.venv/bin/python tools/verify_release.py --profile quick --output-dir validation/stability/quick
```

这会运行全套单元回归和加密协议下的跨屏模型检查。默认不注入系统输入、不控制窗口，也不连接另一台电脑。

原生检查需要显式选择。Mac 检查可能短暂激活自己的测试窗口并捕获鼠标，请保持桌面解锁，运行期间暂勿操作键鼠；切换到其他应用时会中止或失败，并保留结果。

```sh
.venv/bin/python tools/verify_release.py --profile mac-native --allow-native --output-dir validation/stability/mac-native
```

Mac 原生检查包括主线程系统按键解码、普通键/Caps Lock 快速路径、过期回调清理，以及隐藏进入、远控期间关窗/重新打开、焦点丢失和捕获面板跟随位置。窗口场景至少两轮。此层使用本机原生 API 和合成事件，传输对端为替身，**没有真实蓝牙或实体键盘验收**。

所有入口保存机器可读 JSON、可读报告和各项原始日志。只有全部必需项通过才返回 `0`；失败或中断返回 `1`，未测、缺权限、过期或证据不足返回 `2`。子进程返回 `0` 但没有有效结果、阶段为空或被跳过，也不能通过。运行中源码变化会使结果失效。

## Windows 一键入口

生成测试包：

```sh
.venv/bin/python tools/build_validation_bundle.py
```

文件为 `dist/validation/Share-Monkey-Windows-Validation.zip`。在 Windows 解压整个 ZIP，双击 `运行 Windows 自检.cmd`。复用已安装客户端内的 Python 和依赖，不需要再安装 Python，不升级或修改客户端。

先只读观察已安装客户端的连接日志，再加载实际已安装的 0.5.4 输入模块，在独立测试窗口验证固定按键、鼠标定位、左键、滚轮和系统位置回读。按 Esc、关闭测试窗口或切换到其他窗口会停止新的输入；结束时释放测试按键并恢复原指针位置。不在其他应用输入，不读取密码、剪贴板或真实输入内容。结束后自动生成 `Share-Monkey-Windows-Results-时间.zip`，无需手工整理日志。

报告明确列出被测模块来自测试包还是已安装客户端，以及实际版本和文件哈希；不会因为借用了 0.5.4 的 Python，就把 0.5.6 源码结果称为 0.5.4 EXE 整体验收。独立窗口检查不能证明已安装产品的托盘生命周期、真实蓝牙或另一台屏幕的视觉效果。

## 自动混合长跑

```sh
.venv/bin/python tools/soak_stability.py --duration 7200 --output validation/stability/model-soak/report.json
```

默认连续混合执行左右跨屏模型和输入生命周期回归，无系统输入动作。可先用 `--duration 15` 验证工具运行，但短跑通过仅指完成所请求的短跑。报告记录实际成功检查时间、检查点和失败原因，不算作两小时验收。

Mac 增加只读原生键盘事件对象/监听循环检查：

```sh
.venv/bin/python tools/soak_stability.py --scope mac-native --allow-native --duration 7200 --output validation/stability/mac-soak/report.json
```

窗口捕获需再加 `--include-window-capture`，默认长跑不操作窗口。中断后 `--resume` 仅累加已完整通过的检查时间，不累计离线时间；会明确标记非连续运行。失败、未测、时长不足、源码变化均不能通过。

这些长跑验证模型或本机原生层，**没有实际 BLE 双机混合输入，不能满足真实双机的两小时发布门槛**。

## 已安装客户端的只读诊断

Mac 或 Windows 都可执行：

```sh
python tools/collect_paired_evidence.py observe --duration 15 --output validation/stability/peer.json
```

只读取 Share Monkey 自己的进程、窗口是否可见及现有聚合日志。仅当同一进程/同一会话在观察期间双向帧数持续增加，才报告本地观察通过。刚点击等待、历史累计跨屏次数、只有发送没有接收、进程退出或日志达到大小上限，都不能证明连接正常。

可把两端观察合并：

```sh
python tools/collect_paired_evidence.py combine --mac mac-peer.json --windows windows-peer.json --output paired-observation.json
```

合并结果始终是 `observational` 范围，完整双机验收仍为 `not_run`。现有聚合日志没有双方共享的已认证会话标识；相同测试标签和重叠时间不能证明这两个文件来自同一对真实连接。窗口一直隐藏，也不等于已执行关闭/重新打开场景。工具不会自动重连、读取配对密码或接受远程控制指令。

## 发布门槛

```sh
.venv/bin/python tools/verify_release.py --profile release --output-dir validation/stability/release \
  --mac-report validation/stability/mac-native/report.json \
  --windows-report windows-native.json \
  --paired-report paired.json --soak-report real-peer-soak.json
```

发布验收必须同时具有当前源码/测试指纹、对应产品版本、24 小时内有效结果，以及以下证据：

| 层级 | 必需证据 |
|---|---|
| 回归 | 全套回归和跨屏模型完成，无跳过、失败、中断 |
| Mac 原生 | 键盘线程和所有窗口场景完成 |
| Windows 原生 | 在 Windows 实际执行，被测模块与目标版本一致 |
| 真实双机 | 实际 BLE 双向控制、关闭/重新打开窗口、断开后恢复、混合输入场景均完成 |
| 耐久 | 上述真实双机混合输入持续至少两小时，有实际运行时段和覆盖证据 |

当前工具可以自动产生前三层中的对应结果，以及观察/模型长跑报告。**完整真实 BLE 场景驱动和真实双机长跑尚未接入；缺失时发布门槛必须保持未放行。** 不能手工把报告中的范围或状态改为通过来绕过这项限制。

正式打包使用带门禁的入口：

```sh
.venv/bin/python tools/release_client.py --platform macos \
  --output-dir dist/candidate --evidence-dir validation/stability/candidate \
  --mac-report mac-native.json --windows-report windows-native.json \
  --paired-report paired.json --soak-report real-peer-soak.json
```

它先使上次成功结果失效，重新运行完整 release 验收，并核对本轮报告、源码、版本及各项结果；只有通过才调用现有构建器。缺报告、异常退出、超时或中断都不会进入构建。Mac 0.5.6 可选择现有 Windows 0.5.4 作为兼容目标；若构建新的 Windows 0.5.6，必须使用 `--platform windows --windows-version 0.5.6` 及对应真实版本证据，不能借 0.5.4 结果批准新的 Windows 程序。原 `build_client.py` 等保留用于开发构建。

历史故障及现有回归入口：系统按键崩溃见 `test_mac_keyboard.py`；第二次跨屏门控见 `test_mac_media_lifecycle.py`、`test_event_time.py`；边缘跳动和连续往返见 `test_mac_to_windows.py`、`test_switching.py`；窗口常驻见 `test_mac_window_lifecycle.py`、`test_mac_capture_ui.py`；旧危险路径的负控证据仍保存在 `validation/v056/`。
