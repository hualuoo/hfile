# 更新记录

每个发布的版本改了什么。程序在 [Releases](https://github.com/hualuoo/hfile/releases) 下载，升级的做法见[升级](docs/upgrade.md)。

<!-- 每个版本一节，标题只写「## 版本号」：发布流程（.github/workflows/release.yml）照它取出这一节，作为 GitHub 上的发布说明。链接写完整的地址，发布说明里才点得开。 -->

## 1.0.0

第一个公开发布的版本。

- 单文件程序有 Linux x86-64 和 arm64 两种，要 glibc 2.34 以上（Ubuntu 22.04、Debian 12、Rocky/RHEL 9 或更新）。下载和机器对应的 `hfile-linux-x86_64` 或 `hfile-linux-aarch64`（`uname -m` 显示的那种），用 `SHA256SUMS` 核对；名字后面加了 `-licenses.txt` 的是打进程序的第三方库的许可证。
- 还有 Windows x86-64 的 `hfile-windows-x86_64.exe`，只在 Windows Server 2025 上试过，和 Linux 上的不同之处见[在 Windows 上运行](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/deploy.md#在-windows-上运行)。
- 能做什么见 [README](https://github.com/hualuoo/hfile/blob/v1.0.0/README.md) 和[功能说明](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/features.md)，界面有简体中文和英文。
- 存储源从 0.2.0 的 18 种增加到 48 种，新建时按类别分组；分享链接和发布页这些类型只能读，FebBox 不能上传。新加的类型大多只对着照官方文档做的测试服务器试过，没有用真实账号试过，每种的情况写在[功能说明](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/features.md#存储源)里。
- 用过 0.1.0、0.2.0 的，这几处和原来不一样：
  - 网站是 https、存储源给的下载地址却是 http:// 的（比如局域网里用 http 访问的 MinIO 或别的 S3 兼容存储，七牛、又拍云绑的 http 域名），文件改成经过本站下载：https 的页面里用不了 http 的地址，图片和视频显示不出来，浏览器还会拦下载。这样的下载用本站的流量，也受代理下载的连接数和速度限制；不想经过本站，就给存储源换上 https 的地址。
  - Google Drive 里的 Google 文档、表格、幻灯片这类文件，名字加上了导出的扩展名（「计划」显示为「计划.docx」），路径也跟着变了：指向它们的分享、直链、过滤规则和目录密码要重新设。
  - WebDAV 被跳转到别的协议、主机或端口（比如 http:// 跳到 https://）时不再跟过去，而是报错并给出跳转的目标地址：把存储源的地址改成那个就行。
  - MinIO 存储源去掉了「允许本站直传（设置跨域）」按钮：MinIO 没有按存储桶的跨域设置，要设的是整个服务的 `MINIO_API_CORS_ALLOW_ORIGIN`。
  - FTP 的主动模式只接受 FTP 服务器那个地址连过来的数据连接。
  - 从源码运行的，换上新代码以后要再运行一次 `pip install -r requirements.lock`：SMB 用的 smbprotocol 和 pyspnego 是新加的。
- 数据库比 0.2.0 多两个迁移：`siteconf.0002_proxy_limits`、`storages.0002_proxy_limits`（代理下载的连接数和速度限制）。用过 0.1.0、0.2.0 的，换上新程序启动时先备份、再自动升级；从源码运行时先运行 `python -B manage.py upgrade`。见[升级](https://github.com/hualuoo/hfile/blob/v1.0.0/docs/upgrade.md)。
