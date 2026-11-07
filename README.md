# 屏幕接力 · ScreenRelay

<img src="docs/icon.png" width="120" alt="屏幕接力图标">

配合 UU 远程与 BetterDisplay 使用的 macOS 菜单栏工具。连接 iPad 或 iPhone 时接管设备专属虚拟屏，断开后恢复实体屏显示与光标设置。

## 能做什么

- 为每台 iPad/iPhone 绑定独立的 BetterDisplay 虚拟屏，分别记录横竖屏方向、分辨率、HiDPI 大小与光标倍率。
- 远程连接时仅保留目标虚拟屏，提供 iPhone 灵动岛与刘海留白调整；断开后核验并恢复实体屏主屏、坐标位置与显示模式。
- 支持 1×–4× 光标倍率调整，内置 Excel 与 PowerPoint 本机光标加载项启用指引。
- 支持菜单栏面板、开机自启、辅助功能授权引导，以及实体屏强制恢复快捷键（`Control + Option + Command + 9`）。
- 隐藏或最小化 UU 窗口时不丢失连接状态；后台重启或异常断开后保留恢复记录。

## 下载与安装

请前往 [Releases 页面](https://github.com/CMD-cmd/ScreenRelay/releases/tag/v1.7.2) 下载 `ScreenRelay-1.7.2-macOS-arm64.zip`。

- **系统要求**：面向 Apple Silicon 平台，需 macOS 14 或更高版本（本机验证环境为 macOS 27）。当前版本为开发预览版，采用 ad-hoc 签名，尚未获得 Apple 公证。
- **外部依赖**：需安装 Apple Command Line Tools（提供 `/usr/bin/python3`），以及 BetterDisplay（部分功能需其专业版）与 UU 远程。

### 安装流程

1. 解压安装包后阅读说明，运行 `Install.command`。安装脚本将程序与助手写入用户目录并注册登录服务，首次安装默认处于暂停状态。
2. 按应用引导在系统设置中授予屏幕接力“辅助功能”权限。
3. 在 BetterDisplay 设置中开启“允许断开所有已连接的显示器”。
4. 在设备工作台中添加或选择设备，绑定 UU 设备名称并确认分辨率与光标倍率。
5. 开启“自动接力”，连接 UU 后等待虚拟屏接管。

更新版本前，请在旧版中按下 `Control + Option + Command + 9` 恢复实体屏并暂停，安装程序会保留配置与备份。

## Office 光标

在“设备工作台 → 连接与启动 → Office 光标”中准备 Excel 和 PowerPoint 加载项，按指引在 Office 加载项管理器中启用。加载项在 Office 进程内运行本机光标模块，按当前倍率调整显示，不改动全局宏安全设置。部分非位图指针与特定动画可能无法缩放。

## 源码构建与测试

构建需支持macOS 26 界面 API 的 Xcode / Swift 工具链及 Python 3.9+。源码可查阅 [v1.7.2 源码目录](https://github.com/CMD-cmd/ScreenRelay/tree/v1.7.2) 或下载 `ScreenRelay-1.7.2-source.zip`。

```sh
bash script/build_and_run.sh --build-only
python3 script/install.py
```

运行单元测试：

```sh
python3 -m unittest discover -s tests -v
```

本版本共执行 537 项测试，其中 533 项通过，4 项 Office 模板读回检查因本地未安装可选依赖（`olefile`、`oletools`）跳过。新增的 20 项恢复流程测试全部通过。

1.7.2 合入修复：
- 实体屏坐标冲突修复；
- ProMotion 精确模式恢复；
- 虚拟屏关闭后延迟写回稳定核验修复。

## 隐私与权限

辅助功能权限用于识别 UU 会话，macOS 授予的权限本身也允许控制其他应用。配置、设备名称与恢复记录保存在本地 `~/Library/Application Support/UUDisplayAutomation/`，软件不包含云端同步或遥测代码。

公开仓库和安装包不包含本机配置、真实设备身份、日志、备份或验收截图。提交问题前，请检查截图和日志中的用户名、设备名及路径。

## 使用许可

本项目原创部分遵循 [GNU GPLv3](LICENSE.md)（SPDX: `GPL-3.0-only`）。

- 允许商业使用、企业内部使用及收费分发。
- 对外分发受 GPL 约束的修改版本时，须按许可要求向接收者提供相应源码并保持 GPLv3。
- 在组织内部使用或修改并不产生对外公开源码的义务。
- 引用的第三方组件保持原有 MIT 许可，详见[第三方声明](THIRD_PARTY_NOTICES.md)。

Copyright 2026 CMD-cmd and ScreenRelay contributors。

本项目与 UU 远程、BetterDisplay、Apple、Microsoft 无隶属关系。
