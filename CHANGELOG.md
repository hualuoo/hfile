# 更新记录

每个发布的版本改了什么。程序在 [Releases](https://github.com/hualuoo/hfile/releases) 下载，升级的做法见[升级](docs/upgrade.md)。

<!-- 每个版本一节，标题只写「## 版本号」：发布流程（.github/workflows/release.yml）照它取出这一节，作为 GitHub 上的发布说明。链接写完整的地址，发布说明里才点得开。 -->

## 1.1.0

新加了操作日志和 IP 归属地。数据库有变化：停掉旧的程序，换上新的文件，再启动，单文件程序会先把整个数据库备份到 `backups/`（文件名以 `-before-1.1.0.sql.gz` 结尾），再自动升级；从源码运行的，拉了新代码以后先 `pip install -r requirements.lock` 装上新的依赖 maxminddb，再运行 `python -B manage.py upgrade`。升级以后要退回 1.0.1，得用这份备份恢复数据库，只换回旧的程序它不会启动（见[升级](https://github.com/hualuoo/hfile/blob/v1.1.0/docs/upgrade.md)）。

- 操作日志（管理页「用户」下）取代了登录日志。记下的有：登录和没成功的登录、修改或设置密码、开启和关闭两步验证、绑定和解除单点登录、单点登录自动注册；新建文件夹、重命名、移动、复制、删除、上传和在线编辑；生成短链，创建、删除和清理分享；管理员在管理页上的修改（用户和存储源权限，存储源和它的过滤规则、目录密码、目录文档，各页设置，单点登录，删除短链和下载日志，IP 归属地），安装和 `hfile resetadmin`。每一条有时间、IP、浏览器、谁、做了什么、对什么和详情（按页面的语言写出）。密码和别的机密不记，浏览、预览和下载不记（下载在下载日志里），后台里的修改也不记。只能新增，不能修改或删除，也没有开关。可以按操作、存储源、日期筛选，搜索用户名、昵称、存储源名称、路径、Key 或 IP 开头；用户列表每一行有「操作日志」按钮。详见[功能说明](https://github.com/hualuoo/hfile/blob/v1.1.0/docs/features.md#网站管理)。
- **升级时，原来的登录日志搬进操作日志**；「安全设置」里的「登录日志」开关去掉了，登录总是记下。接口 `/api/v1/manage/login-logs/`（列表和删除）换成只能读的 `GET /api/v1/manage/activity/`，安全设置的接口里也没有 `login_log` 了（再发它回答 400），见[接口](https://github.com/hualuoo/hfile/blob/v1.1.0/docs/api.md)。
- IP 归属地（管理页「系统」下，默认关着）：启用以后，在这一页上从两个 MaxMind 的数据库里选一个下载：城市数据库 GeoLite2-City（约 50 MB，很多 IP 还有省和城市），或者国家数据库 GeoLite2-Country（约 3 MB，只有国家或地区，占的内存少得多）。从 [ip.adysec.com](https://ip.adysec.com/) 下载，不用注册 MaxMind 的账号；文件放在新的设置 `[paths] geoip` 指的文件夹里（默认是 `config.toml` 旁边的 `geoip/`），查询在本机完成，IP 不会发给别人。之后用户列表和用户编辑框、操作日志、下载日志和后台的用户页里，IP 旁边显示它在哪里，比如「223.5.5.5 中国 浙江 杭州」「8.8.8.8 美国」，英文界面显示英文名。网站不会自己更新数据库，要点「更新数据库」；启用以前记下的 IP 可以「补全归属地」。
- 公开页面的备案号和「Powered by HFile」一直显示在窗口最底下，滚动时也不动（手机上在底部导航栏上面，播放音频时在播放条上面）；「HFile」链接到项目的 GitHub 页面。

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
