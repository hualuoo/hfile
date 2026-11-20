# 更新记录

每个发布的版本改了什么。程序在 [Releases](https://github.com/hualuoo/hfile/releases) 下载，升级的做法见[升级](docs/upgrade.md)。

<!-- 每个版本一节，标题只写「## 版本号」：发布流程（.github/workflows/release.yml）照它取出这一节，作为 GitHub 上的发布说明。链接写完整的地址，发布说明里才点得开。 -->

## 1.0.0

HFile 的第一个版本。

- 单文件程序有 Linux x86-64 和 arm64 两种，要 glibc 2.34 以上（Ubuntu 22.04、Debian 12、Rocky/RHEL 9 或更新）。下载和机器对应的 `hfile-linux-x86_64` 或 `hfile-linux-aarch64`（`uname -m` 显示的那种），用 `SHA256SUMS` 核对；名字后面加了 `-licenses.txt` 的是打进程序的第三方库的许可证。
- 还有 Windows x86-64 的 `hfile-windows-x86_64.exe`，只在 Windows Server 2025 上试过，和 Linux 上的不同之处见[在 Windows 上运行](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/deploy.md#在-windows-上运行)。
- 能做什么见 [README](https://github.com/hualuoo/hfile/blob/v1.0.0/README.md) 和[功能说明](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/features.md)，界面有简体中文和英文。
- 存储源有 48 种，新建时按类别分组；分享链接和发布页这些类型只能读，FebBox 不能上传。有的类型只对着照官方文档做的测试服务器试过，没有用真实账号试过，[功能说明](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/features.md#存储源)里各自写着。
