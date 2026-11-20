# 更新记录

每个发布的版本改了什么。程序在 [Releases](https://github.com/hualuoo/hfile/releases) 下载，升级的做法见[升级](docs/upgrade.md)。

<!-- 每个版本一节，标题只写「## 版本号」：发布流程（.github/workflows/release.yml）照它取出这一节，作为 GitHub 上的发布说明。链接写完整的地址，发布说明里才点得开。 -->

## 1.0.1

修正和改进。数据库没有变化：停掉旧的程序，换上新的文件，再启动就行（见[升级](https://github.com/hualuoo/hfile/blob/v1.0.1/docs/upgrade.md)）。

- 权限：「预览」和「复制下载链接」现在也要先有「下载」，和「获取直链」「生成短链」一样。预览时浏览器拿到的就是文件本身，只给预览、不给下载挡不住下载，复制下载链接更是换了个名字的下载。**升级以后，有「预览」、没有「下载」的用户不能再预览**，用户页面上也显示为没有「预览」；要让他们继续预览，给他们勾上「下载」。
- 停止网站时（Ctrl+C、SIGTERM，Windows 上还有 Ctrl+Break），还在排队等线程的请求也回答完再停，和正在处理的一样最多等 30 秒。以前同时来的请求比 `[server] threads` 多时，排队的那些会被直接断开。
- 在文件列表或预览里点「下载」记的下载日志，和直链、短链一样，同一个文件、同一个 IP 和用户 10 分钟内只记一条：IDM 这类工具接过下载、分段下载时，不再一次记很多条。
- 程序旁边的 `config.toml` 大多不是 HFile 的节和设置时（比如把程序放在「下载」文件夹里运行，那里正好有别的程序的 `config.toml`），不再把里面每一项当成要改的错列出来，而是提示它多半是别的程序的文件；HFile 不改动它，也不启动。把 HFile 放进一个单独的文件夹里运行，或者用 `--config` 指定设置文件。
- Windows 的程序 `hfile-windows-x86_64.exe` 有了图标，就是 HFile 的 logo。
- [常见问题](https://github.com/hualuoo/hfile/blob/v1.0.1/README.md#常见问题)加了一条：IDM 这类工具提示不能续传、只能单线程下载时，多半是前面的 Nginx 开着缓存（宝塔面板默认就开着），怎么改。

## 1.0.0

HFile 的第一个版本。

- 单文件程序有 Linux x86-64 和 arm64 两种，要 glibc 2.34 以上（Ubuntu 22.04、Debian 12、Rocky/RHEL 9 或更新）。下载和机器对应的 `hfile-linux-x86_64` 或 `hfile-linux-aarch64`（`uname -m` 显示的那种），用 `SHA256SUMS` 核对；名字后面加了 `-licenses.txt` 的是打进程序的第三方库的许可证。
- 还有 Windows x86-64 的 `hfile-windows-x86_64.exe`，只在 Windows Server 2025 上试过，和 Linux 上的不同之处见[在 Windows 上运行](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/deploy.md#在-windows-上运行)。
- 能做什么见 [README](https://github.com/hualuoo/hfile/blob/v1.0.0/README.md) 和[功能说明](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/features.md)，界面有简体中文和英文。
- 存储源有 48 种，新建时按类别分组；分享链接和发布页这些类型只能读，FebBox 不能上传。有的类型只对着照官方文档做的测试服务器试过，没有用真实账号试过，[功能说明](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/features.md#存储源)里各自写着。
