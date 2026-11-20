# 运行

[HFile](../README.md) › [文档](README.md)

`hfile` 不带命令运行就是网站：

```bash
./hfile                                   # 用程序旁边的 config.toml
./hfile --config /srv/hfile/config.toml   # 用别处的设置文件
./hfile upgrade --check                   # 别的命令和 manage.py 的一样
```

从源码运行时，`python -B hfile.py` 和 `python -B manage.py serve` 效果一样。`runserver` 只用来开发。

- 入口：`hfile.py` 和 `manage.py` 都调用 `config/cli.py` 的 `main`：先把标准输出和标准错误改成写得出任何字符（不是终端时用 UTF-8，比如重定向到文件、systemd、docker；编码里没有的字符写成 `\uXXXX`，不会因为一个汉字出错退出），再改用这台系统的时区和 CA 证书（单文件程序），再找到、新建或检查 `config.toml`，然后执行命令：`hfile`（`hfile.py`）不带命令就是 `serve`，`manage.py` 不带命令列出所有命令。命令运行中按 Ctrl+C，只打一行「已中断。」，退出码 130。
- 启动顺序：先占住端口（被占用时直接停下，不碰数据库，比如旧版本还开着），再检查数据库要不要升级（见[「升级」](upgrade.md)），然后开始提供网页。
- 启动时打印用的是哪个数据库（`数据库：SQLite，文件 /srv/hfile/hfile.sqlite3` 或 `数据库：MySQL，127.0.0.1:3306 上的 hfile`）、上传的文件放在哪、网站和后台的地址；还没安装时打印安装页面的地址和安装口令。
- 程序分成两个进程：外面的只管一件事，里面的进程以退出码 3 结束时把它重新启动（安装完成后就靠这个读入新设置）；里面的才是网站。Ctrl+C 两个进程都会收到；外面的进程等 3 秒，再给里面的发 SIGTERM：SIGINT 只发给了外面那个的话（`kill -INT`），里面的这才停下；里面的自己也收到了、正在把请求做完的话，不理这个 SIGTERM。SIGTERM（systemd、`docker stop`）也转给里面的。在 Linux 上外面的进程被强行结束（`kill -9`）时，里面的会收到 SIGTERM 跟着退出（`PR_SET_PDEATHSIG`），不会继续占着端口。单文件程序还多一个进程：运行的那个 `hfile` 是 PyInstaller 的启动进程，外面的进程是它的子进程；启动进程被 `kill -9` 时，外面的进程也收到 SIGTERM，等网站把手上的请求做完一起退出。启动进程把收到的信号都转给它的子进程（外面的进程，或者命令的进程），终端里按一次 Ctrl+C，子进程会收到两次 SIGINT，半秒之内的第二次算作同一次。
- 收到 SIGTERM 或 Ctrl+C 时，不再接新的请求，把手上的请求处理完（最多等 30 秒）再退出；不想等的话再按一次 Ctrl+C，手上的请求就被切断了。启动时正在升级数据库的话，先把数据库恢复到升级前再退出；正在恢复备份时收到的信号要等恢复做完。
- 网页由 waitress（纯 Python 写的 Web 服务器，打包在程序里）提供：不清除代理的请求头（信不信由 `[server] proxies` 决定）；同时最多 1000 个连接（Windows 上 500）：一个连接要占一个文件描述符，发送静态文件时再占一个，所以启动时把能同时打开的文件数（软限制，systemd 默认给服务 1024 个）调高到够用，最多到硬限制；硬限制也不够时连接数相应减少；请求体最多 128 MB，是网站自己上限（64 MB）的两倍：超过 64 MB 时网站回答 413 并说明原因（接口是 JSON，网页是错误页），waitress 只拦更大的。文件会分块上传，用不着更大的请求。超过 512 KB 的请求体，waitress 先写进临时文件夹（`$TMPDIR`，默认 `/tmp`）再交给网站。
- 免得有人开一堆发了一半的请求占着连接，这几种连接会被关掉（不回答）：30 秒没有动静的；请求头在第一个字节以后 30 秒还没发完的（每隔几秒发一个字节也不行）；请求的内容平均每秒不到 1 KB 的（前 30 秒不算，最慢的手机网络也比这快得多）。`[server] proxies = 0`（前面没有代理）时，同一个地址同时最多 100 个连接，多的直接关掉，IPv6 地址按 /64 算；有代理时不限，因为所有连接都来自代理。每 5 秒查一次，所以会晚几秒；被关掉的连接控制台每分钟最多说一次，说有几个。
- 分块发送（`Transfer-Encoding: chunked`）的请求体另有三条上限，超过时回答 400 并断开连接：块大小那一行（连同扩展）最长 4 KB；末尾的 trailer 最长 8 KB；块数最多是 1024 加上前面的块里数据的 KB 数（块大小那一行每 32 字节多算一块，空行也算一块），也就是 1024 块以后每块平均要有 1 KB。curl、requests 这些客户端发的块都是几 KB，碰不到。块大小那一行里也不能有 LF，有就回答 400（waitress 自己会放过行尾的一个 LF，见[「设计说明」](design.md)）。waitress 自己读分块的请求体没有这些上限，而且慢（见[「设计说明」](design.md)），一个这样的请求就能让整个网站卡住。
- 静态文件（CSS、JS、字体、图标）由程序自己提供，关着调试模式也一样。网址带着文件内容的指纹（`?v=…`），指纹对得上时浏览器缓存一年。网站保存的上传文件（比如 Logo）也由程序提供（`/media/…`），只给上传文件夹里、名字符合网站自己的命名规则的文件。
- 每个请求打印一行访问记录，格式参照 Go 的 Gin 等框架：
  ```
  [2026-11-07 23:42:37] INFO access: 200 |    5.4ms |       127.0.0.1 | GET     "/install/?token=<token>" |  12.0KB | "Mozilla/5.0 …"
  ```
  依次是状态码、程序处理用的时间（不含排队和传输）、访问者的 IP（按 `[server] proxies` 取）、方法和地址、回答的大小（边生成边发、事先不知道大小的写 `-`）、浏览器。静态文件也算。地址里的中文照原样显示；换行、终端控制符这类看不见的字符写成 `\n`、`\u001b`，伪造不出一行假记录。后台的地址写成 `/<admin>`，链接里的口令、签名写成 `<token>`、`<sign>` 这样，查询参数里的也一样。`[server] log_requests = false` 关掉。出错的请求（500）还会另外连同错误信息打印一遍。

管理员登录不了（忘了密码、两步验证的手机丢了、忘了登录安全入口）时，在服务器上运行：

```bash
./hfile resetadmin            # 只有一个超级管理员时；有几个时列出来让你选，也可以写上用户名
```

它要求在终端里输入两遍新密码（不显示），然后关掉这个人的两步验证，账号停用了的重新启用，他在别处的登录都失效，最后显示登录地址（设了登录安全入口时就是入口的地址）。只有能登录服务器的人能做这件事，网页上没有重置管理员密码的地方。

上线：

- 用 https 的话，前面放一个 Caddy 或 Nginx 处理证书：`[server]` 写 `host = "127.0.0.1"`、`proxies = 1`，`[site] url` 写 https 的地址。通过代理打开安装页面时，`url` 和 `proxies` 安装向导会填好。
- Caddy 用默认设置就行（`reverse_proxy 127.0.0.1:8080`）。Nginx 要传 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto` 三个请求头，请求体上限和程序的一样：
  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8080;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      client_max_body_size 64m;
  }
  ```
- 不用反向代理、直接对外只适合局域网：在公网上，前面要放一个 Caddy 或 Nginx。上面那几条只挡得住最简单的慢请求，挡不住从很多地址来的大量连接：Caddy、Nginx 同时撑得住成千上万个连接，HFile 只有 1000 个。直接对外时，`allowed_hosts` 要写上大家用来访问的域名或 IP。
- http 跳到 https 和 HSTS 都交给前面结束 https 的代理：HFile 自己不监听 https，也不知道这个域名下还有没有别的服务。Caddy 默认就会把 http 跳到 https，HSTS 加一行：
  ```caddy
  files.example.com {
      reverse_proxy 127.0.0.1:8080
      header Strict-Transport-Security "max-age=31536000"
  }
  ```
  Nginx：
  ```nginx
  server { listen 80; server_name files.example.com; return 301 https://$host$request_uri; }
  server {
      listen 443 ssl; server_name files.example.com;
      # ssl_certificate …; ssl_certificate_key …;
      add_header Strict-Transport-Security "max-age=31536000" always;
      location / { …（上面那一段） }
  }
  ```
  确认 https 一直能用以后再加 HSTS：浏览器记住它以后，在 max-age 之内这个域名就打不开 http 了。没把握时先写 `max-age=300` 试一试。这样的网站上 `hfile check --deploy` 还会报 security.W004（HSTS）和 security.W008（跳转 https），它们由代理负责，可以不管；`[site] url` 不是 https 时还有 W012、W016（Cookie 只走 https），这是故意的，没有证书时也要能登录。自己写的 `secret_key` 不到 50 个字符时还有 W009：程序只要 32 个，Django 的检查要 50 个。
- `/media/`（网站保存的 Logo、图标）程序自己提供，浏览器缓存一年，代理不用管。要让 Nginx 直接从上传文件夹提供它的话，只能在那个文件夹里只有网站自己保存的文件时这样做：程序只给登记过的文件名，代理会把文件夹里的一切都给出去。
- `proxies` 不是 0 而 `host` 是 `0.0.0.0`（所有地址）时，启动时会提醒：绕过代理直接访问端口的人可以伪造 `X-Forwarded-For`。代理在同一台机器上就把 `host` 改成 `127.0.0.1`，不然用防火墙只让代理连这个端口。
- 调试模式保持关闭（默认就是关的）。
- 被「访问控制」的名单挡在网站外面时（比如自己的 IP 变了、落进了名单里的 IP 段），在服务器上运行 `hfile unblock`（从源码是 `python -B manage.py unblock`）清空 IP 和 UA 黑名单，正在运行的网站最多 30 秒后生效。
- 定时清理：`hfile cleanup` 删除过期的东西（现在是过期的会话（登录状态，没登录的人上传、输目录密码用的），24 小时没有动静的上传会话和它们暂存的文件，过期的短链和它们的下载日志，过期的分享，过期的 OnlyOffice 编辑会话，备份被打断时留下的、一天没动过的 `.part` 文件），每一项打印一行删了几个。`--dry-run` 只数不删，`--list` 列出清理项，`--only 名字` 只运行一项；有一项出错时别的照样运行，退出码是 1。没设定时任务时，网站也会在有人上传时顺便清理过期的上传（每个进程最多 10 分钟一次），但还是建议放进 cron，比如每小时一次：
  ```cron
  17 * * * *  /srv/hfile/hfile cleanup --config /srv/hfile/config.toml
  ```
  网站用哪个账号启动，这个定时任务就放进哪个账号的 crontab，命令也用这个账号运行（下面 systemd 的例子里是 `hfile`）：程序建的设置文件和 SQLite 数据库只有这个账号能读写，别的账号运行会出错；root 读得了，但它新建的文件和文件夹网站用不了。
- 上传：没传完的文件块暂存在 `config.toml` 旁边的 `upload-spool/` 里（权限 700，网站不提供它），磁盘要放得下正在上传的文件，再留 64 MB。Nginx 的 `proxy_read_timeout`（默认 60 秒）不用为上传改：大文件最后一块的回答要等网站写进远程存储，被代理切断时面板会查进度，一直等到写完。

用 systemd 运行：网站的进程意外结束时（比如内存不够被系统结束），外面的进程也跟着退出，网站就停了，要由 systemd 重新启动它：

```ini
# /etc/systemd/system/hfile.service
[Unit]
Description=HFile
After=network-online.target mysql.service
Wants=network-online.target

[Service]
User=hfile
ExecStart=/srv/hfile/hfile --config /srv/hfile/config.toml
Restart=on-failure
RestartSec=5
# 停下时 HFile 会把手上的请求做完，最多等 30 秒；正在升级时还要恢复数据库。
TimeoutStopSec=120
# /tmp 是很小的 tmpfs、挂载时带了 noexec，或者会被 systemd-tmpfiles 定期清理时：大的请求体和
# 单文件程序解压出来的文件（约 170 MB）放到别处（先建好这个文件夹，给 hfile 用户写权限）。
# Environment=TMPDIR=/srv/hfile/tmp

[Install]
WantedBy=multi-user.target
```

`systemctl daemon-reload`、`systemctl enable --now hfile` 以后，`journalctl -u hfile` 看输出（访问记录也在里面）。`hfile`（serve）的退出码：0 正常停下（Ctrl+C、SIGTERM）；1 没能启动（端口被占用、设置文件或数据库有问题，原因打印在前面）；2 命令行写错了（比如 `--config` 后面没写路径）；128 加信号的编号：网站的进程被这个信号结束了（137 是 SIGKILL，比如内存不够）。3 只在两个进程之间用（重新读设置），不会传出来。

命令（`upgrade`、`backup`、`restore`、`resetadmin` 这些）也用运行网站的账号 hfile 运行，`upgrade` 和 `restore` 要先 `systemctl stop hfile`；定时清理也放进这个账号的 crontab（`crontab -u hfile -e`）：

```bash
sudo -u hfile /srv/hfile/hfile --config /srv/hfile/config.toml backup
```

用 root 运行的话，命令新建的文件和文件夹归 root、只有 root 能读写，网站用不了它们。比如第一次 `backup` 建的备份文件夹：以后换新版本时，网站启动前的自动备份写不进去，就不升级，网站也起不来（提示里会说是哪个文件夹）。已经这样了的话，把它交还给这个账号：`chown -R hfile: /srv/hfile/backups`。

## 在 Windows 上运行

Windows 的单文件程序 `hfile-windows-x86_64.exe` 是 x86-64 的，只在 Windows Server 2025 上试过。改名成 `hfile.exe` 放进一个文件夹，在 PowerShell 或命令提示符里运行，命令和 Linux 上一样：

```powershell
.\hfile.exe                                  # 用程序旁边的 config.toml
.\hfile.exe --config D:\hfile\config.toml    # 用别处的设置文件
.\hfile.exe upgrade --check                  # 别的命令也一样
```

和 Linux 上不一样的地方：

- 文件的权限：设置文件、SQLite 数据库、安装口令文件、备份和上传时暂存的文件，程序建它们的时候就只让运行它的账号、SYSTEM 和 Administrators 读写（Linux 上的 600、700），不管文件夹给了谁。SQLite 在数据库旁边写的 `-wal`、`-shm` 文件却跟着文件夹的权限：数据库所在的文件夹别的用户也能读时（比如在 `C:\` 下新建的文件夹，默认这台机器的用户都能读），程序启动时会提醒，并给出一条 `icacls` 命令，把文件夹改成只有这三者能用。放在自己的用户文件夹里（比如 `C:\Users\用户名\hfile`）就没有这个问题。上传的文件、本地存储源里的文件照文件夹的权限。
- 停下：在程序的窗口里按 Ctrl+C 或 Ctrl+Break，和 Linux 上的 Ctrl+C、SIGTERM 一样，把手上的请求做完再退出，退出码 0。Windows 没有 SIGTERM：别的程序要它这样停下，也要给它发 Ctrl+C 或 Ctrl+Break；`taskkill /F` 这类强行结束不等手上的请求。关掉窗口也会结束程序，手上的请求可能被切断。
- 进程：和 Linux 上一样分外面的进程和网站两个进程，单文件程序还多一个启动进程，任务管理器里是三个 `hfile.exe`。外面的进程把自己和网站放在一个作业对象（job object）里：外面的进程不管怎么结束，网站都跟着结束。启动的那个 `hfile.exe` 被强行结束时（任务管理器、`taskkill /F`），外面的进程和网站马上跟着结束，不会占着端口，但手上的请求会被切断（Linux 上会等它们做完）。这时程序解压出来的临时文件夹（`%TEMP%\_MEI…`，约 80 MB）没人删，要自己删。
- 从别的电脑访问：Windows 防火墙可能挡住别的电脑连进来，要放行程序的端口（以管理员身份运行 PowerShell）：
  ```powershell
  New-NetFirewallRule -DisplayName HFile -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
  ```
- 开机自动运行：程序不是 Windows 服务，不能直接用 `sc.exe create` 注册，要借助把普通程序包装成服务的工具，见下面的 WinSW。
- 本地存储源：
  - 新的名字里不能有 `< > : " \ | ? *`，不能以点或空格结尾，也不能是设备名（`CON`、`NUL`、`COM1` 这些，带扩展名也不行）：这样的名字 Windows 不允许，或者别的程序打不开。
  - 符号链接和目录联接（junction）不能移动到另一个磁盘（Linux 上会照着它在那边再建一个链接）。移动到另一个磁盘时，文件和文件夹的修改时间也不保留（Linux 上保留）。
  - 路径里的 8.3 短名（比如 `PROGRA~1`）找不到东西，只认长名字；链接的目标里写的是 8.3 短名时，这个链接也打不开。
  - 覆盖一个文件时，新的文件照它所在文件夹的权限，原来那个文件自己另设的权限不保留（Linux 上保留原来的权限位）。

### 作为 Windows 服务运行

下面用 [WinSW](https://github.com/winsw/winsw)（试过 2.12.0 的 `WinSW-x64.exe`）把程序注册成服务 HFile，以服务自己的虚拟账号 `NT SERVICE\HFile` 运行，不用权限最大的 LocalSystem。程序和它的文件放在 `C:\hfile`：

1. 把 `hfile.exe` 放进 `C:\hfile`；从 WinSW 的 Releases 下载 `WinSW-x64.exe`，改名成 `hfile-service.exe` 放在旁边，再在旁边写一个 `hfile-service.xml`：
   ```xml
   <service>
     <id>HFile</id>
     <name>HFile</name>
     <description>HFile</description>
     <executable>%BASE%\hfile.exe</executable>
     <stopparentprocessfirst>true</stopparentprocessfirst>
     <stoptimeout>120 sec</stoptimeout>
     <onfailure action="restart" delay="10 sec"/>
     <log mode="roll-by-size">
       <sizeThreshold>10240</sizeThreshold>
       <keepFiles>8</keepFiles>
     </log>
   </service>
   ```
   停止服务时 WinSW 先给程序发 Ctrl+C，程序把手上的请求做完（最多 30 秒）、正在升级时还要恢复数据库，所以 `stoptimeout` 给得长一些，超时才强行结束。程序意外退出时（比如网站的进程被结束），10 秒后重新启动。
2. 以管理员身份运行 PowerShell：
   ```powershell
   cd C:\hfile
   .\hfile-service.exe install
   sc.exe config HFile obj= "NT SERVICE\HFile"
   # 文件夹只给服务账号、SYSTEM 和 Administrators：程序的文件都放在这里
   icacls C:\hfile /inheritance:r /grant:r "NT SERVICE\HFile:(OI)(CI)M" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
   Start-Service HFile
   Get-Content C:\hfile\hfile-service.out.log -Encoding UTF8   # 安装页面的地址和口令
   ```
3. 用浏览器打开日志里的地址装好。服务自己读入新的设置，不用重启服务。

以后用 `Stop-Service HFile`、`Start-Service HFile`、`Restart-Service HFile` 停止、启动、重启；不要了就 `Stop-Service HFile`，再 `.\hfile-service.exe uninstall`。程序的输出在 `C:\hfile` 里：`hfile-service.out.log` 是启动时打印的那些，`hfile-service.err.log` 是访问记录和出错时的消息，`hfile-service.wrapper.log` 是 WinSW 自己的；每个满 10 MB 换一个新的，留 8 个。它们是 UTF-8 的，Windows PowerShell 5.1 的 `Get-Content` 要加 `-Encoding UTF8`。程序解压到服务账号的临时文件夹 `C:\Windows\ServiceProfiles\HFile\AppData\Local\Temp`。

- 命令（`upgrade`、`backup`、`restore`、`resetadmin`）在以管理员身份运行的 PowerShell 里、在 `C:\hfile` 里运行：管理员能读写程序的文件。`upgrade` 和 `restore` 要先 `Stop-Service HFile`。命令新建的文件和文件夹只给运行它的管理员、SYSTEM 和 Administrators：比如第一次 `backup` 建的 `backups` 文件夹，服务账号写不进去，以后升级前的自动备份就会失败。所以以管理员身份运行过命令以后，再给服务账号加上它们：
  ```powershell
  icacls C:\hfile /grant "NT SERVICE\HFile:M" /T
  ```
  已经在控制台里装好、再改成服务的，也在第 2 步的 `icacls` 之后运行一次这条（先停掉控制台里的程序）。
- 换新版本：`Stop-Service HFile`，换掉 `hfile.exe`，再 `Start-Service HFile`，启动时先备份再升级数据库（见[「升级」](upgrade.md)）。
- 试过的：服务启动、走完安装向导、服务自己重启读入新设置、`Stop-Service` 正常停下（退出码 0）、`Restart-Service`；网站的进程或启动的那个 `hfile.exe` 被强行结束时，服务 10 秒后自动重新启动。机器重启后自动启动（启动类型是「自动」）没有试过。
