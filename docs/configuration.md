# 设置文件

[HFile](../README.md) › [文档](README.md)

程序的设置都在 `config.toml` 里（TOML 格式，每一项上面都有说明；`config.example.toml` 就是一份新的）：

| 节 | 设置 |
|---|---|
| `[server]` | `host` 监听的 IP（默认 `0.0.0.0`，所有 IPv4 地址；`::` 是所有 IPv6 地址）、`port` 端口（默认 8080）、`threads` 同时处理几个请求（默认 8；经过本站的下载用自己的线程，不算在里面）、`proxies` 前面有几层反向代理（默认 0）、`log_requests` 每个请求打印一行访问记录（默认开） |
| `[database]` | `engine` 数据库的类型：`"sqlite"`（默认）用 `file` 数据库文件（默认 `hfile.sqlite3`）；`"mysql"` 用 `host`、`port`、`name`、`user`、`password`，`tls` 连接要不要加密、校验证书（默认 `"preferred"`），`ca` 校验证书用的 CA 证书文件 |
| `[site]` | `secret_key` 密钥、`allowed_hosts` 网站的域名或 IP、`url` 网站对外的地址、`admin_path` 后台的地址、`debug` 调试模式 |
| `[paths]` | `uploads` 网站保存的上传文件（默认 `media/`，和网址 `/media/` 一致）、`backups` 数据库备份（默认 `backups/`）。相对路径从 `config.toml` 所在的文件夹算起，路径里可以有空格 |
| `[upgrade]` | `auto` 启动时自动升级（不写时：单文件程序开，从源码运行关，见[「升级」](upgrade.md)） |

每一项设置在代码里只定义一次：`config/configfile.py` 的 `OPTIONS` 表，一行写着节、名字、默认值、检查规则和新文件里它上面的说明。说明写的是英文原文（中文在 `config/locale/zh_Hans/`），写文件时才翻译：命令行新建的按系统的语言（`LANG=en_US.UTF-8` 这样的是英文，见[「开发：翻译」](development.md#翻译)），安装向导写的按页面的语言。新文件和 `config.example.toml`（中文）都由这张表生成（有测试保证 `config.example.toml` 和它一样），检查文件、写文件也都照它。

用哪个文件：

1. 命令行上的 `--config 路径`，放在命令前后都行。也可以是一个文件夹，表示它里面的 `config.toml`。
2. 没有的话，用环境变量 `HFILE_CONFIG`。
3. 再没有，就用程序旁边的 `config.toml`：单文件程序所在的文件夹（看 `sys.executable`，不看当前目录，也不是程序启动时解压出来的临时文件夹），从源码运行时是 `manage.py` 所在的文件夹。

文件不存在就自动新建一个（权限 600），里面是默认值，`secret_key` 空着，所以程序以安装模式启动。`--version` 和帮助不会新建。不读 `.env`，也不用环境变量写设置。

启动时检查整个文件：拼错的设置名、多出来的节、类型不对（比如端口写成了 `"8080"`）、超出范围的值，会一次全部列出来，程序不启动。文件里大多不是 HFile 的节和设置时（比如程序放在「下载」文件夹里运行，那里正好有别的程序的 `config.toml`，其中有几项和 HFile 的同名也一样），只提示它多半是别的程序的文件，不改动它，程序也不启动：把 HFile 放进一个单独的文件夹里运行，或者用 `--config` 指定别的设置文件。密码和密钥写错时不会把它们的值打印出来。改了设置要重新启动程序。

要注意的地方：

- `allowed_hosts` 和 `url` 里可以直接写中文域名，程序像浏览器一样把它换成 `xn--` 开头的形式（安装向导写进文件的也是这种形式）。个别浏览器能打开、但按 IDNA 2008 转不了的名字（比如带 ☃ 这类符号的），直接写 `xn--` 开头的形式。
- `url` 以 `https://` 开头时，登录用的 Cookie 只走 https；它的域名会自动算进 `allowed_hosts`，也算作 CSRF 信任的来源；完整的链接（直链、短链、分享等）也用它。还没有 https 时不要这样写，不然登录不了。
- `proxies` 不是 0 时，访问者的 IP 取 `X-Forwarded-For` 里从右数第 `proxies` 个（左边那些是客户端想写什么就写什么），是不是 https 看 `X-Forwarded-Proto`；是 0 时这些请求头一概不信。频率限制、访问记录、登录 IP 用的都是这一条规则。
- MySQL 的 `tls`：`"preferred"` 是 MySQL 自己的默认，服务器开着 TLS 就加密，但不校验证书，中间的人可以让连接退回明文；`"required"` 一定加密，不能加密就不连，但还是不校验证书；`"verify_ca"` 用 `ca` 校验服务器的证书是不是这个 CA 签发的；`"verify_identity"` 还要证书上的名字和 `host` 一样。MySQL 在另一台机器上时用后两种，`ca` 写 CA 证书文件（PEM 格式）的路径，相对路径从 `config.toml` 所在的文件夹算起；MySQL 自己生成的证书，CA 是它数据目录里的 `ca.pem`，证书上的名字不是 host，只能用 `"verify_ca"`。`ca` 只在这两种时才有用，`tls` 是别的时写了 `ca`，程序不启动并说明。
- SQLite 的数据库文件要放在这台机器自己的硬盘上，不能放在 NFS、SMB 这类网络共享的文件夹里。它旁边会多出同名的 `-wal`、`-shm` 文件（也是数据库的一部分）和升级用的锁文件 `.lock`。程序运行时不要只复制 `.sqlite3` 文件当备份，用 `backup` 命令。
- 安装向导会按模板重写整个文件：先写 `config.toml.part`，读回来和要写的值比对，再换掉原来的文件。值都保留，自己写的注释会丢掉。
- 文件里有数据库密码和密钥，不要发给别人，也不要放进代码仓库（`.gitignore` 已经排除了它和 SQLite 的数据库文件）。
- 存储源的密钥（SecretKey、密码、令牌）在数据库里是用 `secret_key` 加密保存的，所以数据库和它的备份里没有明文的密钥。反过来，换了 `secret_key` 以后这些密钥就读不出来了：存储源的编辑页上会标出哪一项「读不出来了」，重新填写（网盘重新授权、115 重新扫码）再保存；在那之前用到这个存储源时，列表上它标着「出错」和原因。
- 用 OnlyOffice 时要写 `url`：OnlyOffice 服务器从这个地址下载要打开的文件，保存时也回调这个地址（`/onlyoffice/callback/`）。没写时用浏览器打开网站的那个地址，网站在反向代理后面、OnlyOffice 在另一台机器或 Docker 里时，它多半访问不到（Docker 里的 OnlyOffice 访问不了 `127.0.0.1`、`localhost`）。网站是 https 的，OnlyOffice 也要是 https 的。
- 后台的地址 `admin_path` 也不能和存储源的别名、直链的地址前缀一样（它们在数据库里，在管理页里改）。启动时检查：冲突了 `hfile`（serve）不启动并说明，`runserver` 和 `check` 只提醒。
- 网站自己的设置（名称、Logo、公告、直链前缀、黑名单等）不在这个文件里：它们在数据库里，在管理页改，改了马上生效。换了 `secret_key` 以后，用它加密保存的都读不出来了：存储源的密钥（上一条）、OnlyOffice 的 Secret 和单点登录的 Client Secret 要重新填，要密码的分享要重新创建，开着两步验证的超级管理员要先运行 `hfile resetadmin`；所有人也要重新登录，本站的签名链接（包括永久的）全部失效，没写 `admin_path` 时后台的地址也变了。
