# 打包

[HFile](../README.md) › [文档](README.md)

`scripts/build.sh` 用 PyInstaller（试过 6.22）的 `--onefile` 生成 `dist/hfile`，和它的许可证文件 `dist/hfile-licenses.txt`。打包的是最后一次提交（`git archive HEAD` 解到临时文件夹），所以要在 git 仓库里运行；工作区里没提交的改动不会打包进去，脚本会提醒一句。要在装好依赖的 Python 环境里运行（`conda activate hfile`），系统里还要有 binutils（PyInstaller 在 Linux 上要用 `objdump`）；在哪个系统上打包，程序就在哪个系统上用：glibc 只能向后兼容，所以要在想支持的最老的 Linux 上打包。现在打包进去的库里，Python 和大多数库只要 glibc 2.17，cryptography 和 bcrypt（SFTP 用的 paramiko 要它们）的 Rust 扩展要 2.34，所以程序要 Ubuntu 22.04、Debian 12、Rocky/RHEL 9 或更新的系统。Windows 上在 Git for Windows 的 bash 里运行它，生成 `dist/hfile.exe`，不用 binutils。打包时要注意：

- 每个应用（`settings.HFILE_APPS`，加上 `apps.installer`、`apps.core`、`config`、`apps.storages`）都加 `--collect-submodules` 和 `--collect-data`，否则会漏掉迁移、模板和静态文件；DRF、WhiteNoise、waitress 和读 IP 归属地数据库的 maxminddb 也加 `--collect-submodules`，DRF 还加 `--collect-data`（它的模板和静态文件）。图片验证码要 `captcha` 库带的字体（`--collect-data captcha`）。新加的应用写进 `HFILE_APPS` 就会被打包。
- 应用文件夹里的文件都会作为数据文件打包进去，所以不打包工作区：留在那里的 `.env`、编辑器的临时文件、密钥都不会跟着进程序。测试模块不打包（它们引用只给测试用的库，moto、boto3、WebDAV 和 FTP 服务器等，约 40 MB）；DRF 的测试客户端 `rest_framework.test` 和它带进来的 requests、urllib3、pyOpenSSL 也不打包；测试用的 Flask、WsgiDAV 带进来的 jinja2、markupsafe、PyYAML、click，和只给测试用的 tblib，也不打包（Django 的 Jinja2 模板、YAML 序列化和测试运行器、httpx 的命令行会可选地 import 它们，HFile 都不用）。
- PyInstaller 的 Django 钩子打包时会读设置：`build.sh` 给它一个带密钥的临时 `config.toml`（看到的是装好的网站），不会打包进去。钩子只打包设置里写的那一种数据库的驱动，所以 SQLite 和 MySQL 的都手动加上（`--hidden-import MySQLdb`）。
- 时区和 CA 证书：打包出的 Python 和 OpenSSL 带着打包那台机器上的路径，到了服务器上找不到。所以程序启动时先用系统的（`config/frozen.py`）：时区在 `/usr/share/zoneinfo` 等处，CA 证书在各个发行版放证书的那个文件里；系统里没有时（比如精简的容器镜像）用打包进去的 `tzdata` 和 `certifi`，C 库（日志里的时间）也通过 `TZDIR` 用 tzdata 的。设了 `PYTHONTZPATH`、`TZDIR`、`SSL_CERT_FILE` 或 `SSL_CERT_DIR` 的话照用。
- `libxcb`（Linux）：conda-forge 的 Pillow 依赖它，PyInstaller 却不打包它，没有桌面的服务器上程序会启动不了。`build.sh` 用 `ldd` 找到它和 `libXau`、`libXdmcp`，用 `--add-binary` 加进去。
- 程序图标（Windows）：`build.sh` 在 Windows 上用 `--icon` 加上 `scripts/hfile.ico`，就是 HFile 的 logo，网站的图标 `apps/web/static/web/favicon.svg`。它由 `python scripts/windows_icon.py` 生成：用 Chrome 把 `favicon.svg` 按 Windows 用到的每个尺寸（16 到 256 像素）分别画一张再合成，小的不是从大的缩小的。改了 `favicon.svg` 就重新运行它，把新的 `hfile.ico` 一起提交。Linux 的程序没有图标。
- 打包前先检查环境：`scripts/lock.py --check` 对照提交里的 `requirements.lock`，有一个包的版本不一样（或者多了、少了）就不打包。`requirements.lock` 是测试和打包用的环境里全部的 Python 包，直接的和间接的（`requirements.txt`、`requirements-dev.txt` 要的，加上 conda 装的 mysqlclient 和 Pillow，以及它们一层层要的），各自的版本：`requirements*.txt` 只写了直接依赖的版本，间接依赖装哪个版本由安装那天决定。照它装：`pip install -r requirements.lock`。换版本：装好、两种数据库上跑完测试，再运行 `python scripts/lock.py` 重新生成，和改动一起提交。
- 先打到临时文件夹里，`build.sh` 自己检查：让程序找不到系统的时区（`PYTHONTZPATH` 指向空文件夹），加载北京时间、确认有 CA 证书、两种数据库的驱动和 maxminddb 都在、只给测试用的库都不在程序里；再在空库上分别用源码和打包出的程序跑 `upgrade --check`，列出的迁移必须一样（现在是 41 个），不一样就报错。然后用打包出的程序自己的命令建一个 SQLite 的站（`upgrade`、`createsuperuser`），启动它，请求首页和它引用的样式表、`/api/v1/site/`，登录，再用 SIGTERM（Windows 上是 Ctrl+Break）停下，退出码要是 0：漏打包的模块或者数据文件在这里就会露出来。都通过了，才把程序和许可证文件换进 `dist/`；有一项不通过，原来的 `dist/hfile` 不动。
- 许可证文件里是 HFile 自己的 `LICENSE` 和 `NOTICE`、Python 的许可证，和打进程序的每个 Python 库的许可证文件（`scripts/licenses.py`：按 PyInstaller 工作文件夹里 `.toc` 文件列出的文件找出它们属于哪个库，连同库里带的别人的，比如 Django 后台里的 jQuery）。发布程序时把它一起发布。打进去的本地库（OpenSSL、SQLite、MySQL 的客户端库、Pillow 用的图片库，来自系统或者 conda）只列出名字，许可证要看各自的项目。
- 在这台机器上直接试打包出的程序发现不了路径的问题，因为 conda 的文件夹就在这里：要用 `bwrap --dev-bind / / --tmpfs ~/miniforge3 -- dist/hfile` 把它藏起来试。

打包出的程序试过：在藏起 conda 的沙盒里、在 Ubuntu 24.04（没有时区和 CA 证书文件）和 Oracle Linux 9（glibc 2.34）的干净容器里，SQLite 和 MySQL 各从空文件夹走完安装向导的每一步、装完自动重启、管理员登录网站和后台、`/admin/` 是 404、访问记录里口令和后台地址打码。Windows 的在 Windows Server 2025 上打包，在同一台机器上试过：从空文件夹用 SQLite 走完安装向导、装完自动重启、管理员登录网站和后台、`/admin/` 是 404、访问记录里口令和后台地址打码，在它的窗口里按 Ctrl+C 和发 Ctrl+Break 都正常停下，启动的那个 `hfile.exe` 被强行结束时另外两个进程也跟着结束；MySQL 没有在 Windows 上试过。

## 发布

发布由 GitHub Actions 做（`.github/workflows/release.yml`），推送 `v` 加版本号的标签时运行：

1. 改 `config/version.py` 的 `VERSION`：每次发布都改（见[升级](upgrade.md)的「发布新版本时」）。在 `CHANGELOG.md` 里加一节 `## 版本号`，写这个版本改了什么，它就是 GitHub 上的发布说明；README 和 `docs/README.en.md` 的版本徽章也改成新的版本号。提交，推到 main。
2. 打标签、推送：`git tag v1.0.0 && git push origin v1.0.0`。
3. 工作流先核对标签、`VERSION` 和 `CHANGELOG.md`，对不上就停下；再在 SQLite 和 MySQL 上跑全部测试（`tests.yml`，x86-64 和 arm64 的机器上各一遍，Windows 上 SQLite 一遍）；然后 x86-64 和 arm64 各在自己的机器上打包（PyInstaller 只能打包出它所在那种机器的程序）：在 Oracle Linux 9（glibc 2.34）的容器里，照[开发](development.md)的步骤建环境、装 `requirements.lock`，运行 `scripts/build.sh`，核对打出的程序 `--version` 是这个版本，再按 `uname -m` 改名成 `hfile-linux-x86_64`、`hfile-linux-aarch64`，许可证文件是这个名字后面加上 `-licenses.txt`。Windows 的在 Windows Server 2025 的机器上照样打包（`build.sh` 在 Git for Windows 的 bash 里运行），改名成 `hfile-windows-x86_64.exe`，许可证文件是 `hfile-windows-x86_64-licenses.txt`。
4. 最后建一个草稿 Release：标题是「HFile 版本号」，说明是 `CHANGELOG.md` 里的那一节，附上三种程序、它们的许可证文件，和列出这六个文件的 `SHA256SUMS`。在 GitHub 的 Releases 页面看过以后，点「Publish release」才公开。
