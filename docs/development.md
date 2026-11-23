# 开发

[HFile](../README.md) › [文档](README.md)

## 本地运行

需要 conda；用 MySQL 时还要 Docker。

```bash
conda env create -f environment.yml     # 第一次
conda activate hfile
pip install -r requirements.lock        # 间接依赖也换成测试过的版本（见「打包」）
python -B manage.py check               # 没有 config.toml 时新建一个（默认 SQLite）
```

有两种机器不一样（PyPI 上锁定的 cryptography 50.0.2 没有给它们的 wheel，pip 要从源码编译，见[打包](build.md)）：

- Intel 的 Mac：先装好 Rust（rustup）和 Xcode 的命令行工具，建环境时让 pip 用环境自己的 OpenSSL 编译：`OPENSSL_DIR="$(conda info --base)/envs/hfile" conda env create -f environment.yml`。
- Windows arm64：conda-forge 那里没有 Python 3.12，用不了 conda。装一个 Python 3.12（CI 用 python-build-standalone 的，见 `.github/actions/windows-arm64-python`），在它的 venv 里 `python -m pip install -r requirements.lock`，mysqlclient 和 Pillow 也从 PyPI 装；编译 cryptography 要有 Rust、Visual Studio 的 C++ 工具和静态的 OpenSSL：`vcpkg install openssl:arm64-windows-static-md`，再把 `OPENSSL_DIR` 设成 vcpkg 的 `installed\arm64-windows-static-md`、`OPENSSL_STATIC` 设成 1。

开发用的 `config.toml` 改两处：`[site]` 的 `secret_key`（至少 32 个字符，比如 `python -c "import secrets; print(secrets.token_urlsafe(50))"` 的输出）和 `debug = true`。然后：

```bash
python -B manage.py upgrade             # 建表（和以后的升级是同一个命令）
python -B manage.py createsuperuser     # 第一个管理员
python -B manage.py runserver
```

打开 http://127.0.0.1:8000/ 。管理页是 `/manage/`；调试模式下还有 `/components/`，浅色、深色两种主题并排列出所有组件，写页面时照着用。后台的地址是随机的，`runserver` 启动时会打印（「后台（Django 管理后台）：http://…」）。API 在 `/api/v1/`，调试模式下可以直接在浏览器里浏览。

用 MySQL 时，先起本地的 MySQL 8.4 容器，再让设置文件指向它：

```bash
scripts/dev-db.sh up                         # 第一次会建容器 hfile-mysql，账号写进 dev-db.env（权限 600）
scripts/dev-db.sh use config.toml hfile_dev  # 把 config.toml 的 [database] 改成这个容器里的库 hfile_dev
```

`use` 只改设置文件，不建库：容器里只有 `hfile` 一个库。用别的库名时先建好它：`scripts/dev-db.sh shell` 里执行 `CREATE DATABASE hfile_dev;`，再 `python -B manage.py upgrade`。跑测试不用建，Django 自己建 `test_` 开头的库。

容器只监听 `127.0.0.1:23306`，数据在卷 `hfile-mysql-data` 里。里面的用户能用所有名字以 `hfile` 开头的库，和 Django 测试用的 `test_hfile…` 库，所以几个人、几份代码可以各用各的库名。账号文件默认是项目里的 `dev-db.env`，用环境变量 `HFILE_DEV_DB_ENV` 可以换一个；文件里是 `HFILE_DEV_DB_HOST`、`HFILE_DEV_DB_PORT`、`HFILE_DEV_DB_USER`、`HFILE_DEV_DB_PASSWORD`，别的脚本可以 `source` 它。`dev-db.sh` 还有 `down`（停止，数据保留）、`status`、`logs`、`shell`（mysql 客户端）和 `destroy`（删除容器和数据，会先问）。

在开发机上试安装向导，不动开发用的设置和数据库：

```bash
mkdir -p /tmp/hfile-try
python -B manage.py runserver 8001 --config /tmp/hfile-try/config.toml
```

`--config` 指的文件不存在时会新建，`secret_key` 空着，所以打开的是安装向导；上传的文件、备份、安装口令和 SQLite 的数据库文件都放在它旁边。要试 MySQL，填一个还不存在的库名，比如 `hfile_try`，安装时会建好它。

从源码运行时自动升级默认关着：拉了带新迁移的代码以后，先 `python -B manage.py upgrade`（会先备份）。`runserver` 遇到要升级的数据库只提醒，`serve` 会停下并说明。

## 翻译

页面、脚本里的文字和命令的输出有简体中文和英文两种（`settings.LANGUAGES`）。代码里写的是英文原文，中文在 `config/locale/zh_Hans/LC_MESSAGES/` 里：`django.po` 是 Python 和模板的，`djangojs.po` 是脚本的。

- 页面用哪种：访客在顶栏的语言菜单里选的（存在 Cookie `hfile_language` 里），没选过就按浏览器的 Accept-Language，两种都不是时用中文。命令的输出、启动前的错误和新建的 `config.toml` 按系统的语言：`LANGUAGE`、`LC_ALL`、`LC_MESSAGES`、`LANG` 里第一个设了的，`en` 开头的是英文，别的是中文（`config/i18n.py`）。
- 命令的帮助整个按系统的语言：argparse 和 Django 自己写的英文（用法、标题、参数出错时的消息，Django 命令的说明、选项和占位，`hfile help` 里命令的分组）也翻译。`config/cli_help.py` 在显示的地方到翻译里查，`config/cli_texts.py` 照它们代码里的原样列出这些文字，给 makemessages 提取；英文的帮助还是它们原来的样子。占位（`--output` 的 `OUTPUT`）也翻译，代表数字的 `N` 照旧。中文按终端里占两格算，对齐和换行才不乱。升级 Python 或 Django 以后，`config/tests.py` 会指出帮助里新出现、`cli_texts.py` 里还没有的文字，和 `cli_texts.py` 里已经不出现的：照原样改 `cli_texts.py`，再提取、翻译。
- 写新的文字：
  - Python：`gettext`（一般导入成 `_`）；导入模块时就要算好的（模型的字段名和选项、模块里的常量）用 `gettext_lazy`；带数量的用 `ngettext`。占位写成 `%(名字)s`，不用 f-string。`config/` 里在 Django 启动前也会运行的代码用 `config.i18n._`。
  - 模板：`{% translate "…" %}`，带变量的用 `{% blocktranslate %}`。文字里单引号和双引号都有时也用 `{% blocktranslate %}`：makemessages 读不了 `{% translate %}` 里转义的引号。模板里不用 `{% load i18n %}`。
  - 脚本：`gettext('…')`、`ngettext('…', '…', n)`，带变量的用 `interpolate(gettext('Item %(n)s'), { n }, true)`，不用模板字符串拼文字。这些函数由 `{% js_catalog %}` 在别的脚本之前载入。
  - 一句话是一条，不拼半句：占位里可以放一整句（`%(problem)s`），不放半句；列表用 `_(", ")` 连起来（中文是「、」）。几行的消息每行一条，在外面用换行连起来；一句话本来就跨两行的（比如 `config.toml` 的说明）才在一条里换行。
  - 惰性的翻译（`gettext_lazy` 这类）不能直接用 `%` 或 `+`：那样会马上按导入时的语言翻译掉。DRF 字段的消息用字段自己的占位（`{max_length}`、`{min_value}`，出错时才填），别处用 `format_lazy`。
  - 存进数据库、以后再显示的文字用 `gettext_noop` 标出来，存原文，显示时再翻译。`config/` 里的 `config.i18n` 也有 `ngettext` 和 `gettext_noop`。
  - 同一句英文在不同地方要译成不同的中文时加上下文：`pgettext("track", "Next")`（下一首）、`pgettext("image", "Next")`（下一张），模板里是 `{% translate "Back" context "wizard" %}`（上一步）。
  - 中文只有一种复数形式（`nplurals=1`）：`ngettext` 的中文翻译只有一条，要写成数量是几都通顺的说法。
  - 日志用英文写，不翻译：在请求的线程里翻译会是访客的语言。程序要读回来的数据（备份文件开头的几行，存进数据库的根路径名称默认值「首页」）也不翻译，照原样是中文：语句的第一行加上注释 `# i18n: data`，检查就不把它里面的中文算作问题。
  - 命令的 `help` 写成属性（`@property` 里 `return _("…")`）：argparse 收不了惰性的翻译，这样也按命令的语言显示。
- 然后更新翻译：

  ```bash
  python scripts/i18n.py extract   # 代码里的新文字进 .po，不再用的去掉（要有 GNU gettext）
  # 在 config/locale/zh_Hans/LC_MESSAGES/*.po 里填好 msgstr
  python scripts/i18n.py compile   # 生成 .mo 和 apps/web/static/web/i18n/*.js
  ```

  GNU gettext 可以用 `apt install gettext`、`brew install gettext` 或 `conda install -c conda-forge gettext` 装。`.po`、`.mo` 和 `.js` 一起提交。`apps/web/test_i18n.py` 检查代码里没有中文（数据和它列出的几处除外）、没有对惰性翻译直接用 `%` 或 `+`、每一条都有中文、编译出的文件和 `.po` 一致。

## 测试

```bash
python -B manage.py test
```

测试用设置文件里的数据库，所以先准备一个填了 `secret_key` 的 `config.toml`，用 `HFILE_CONFIG` 或 `--config` 指给它：

- SQLite：`engine = "sqlite"` 就行，Django 用内存里的数据库，不碰设置里的那个文件。
- MySQL：`scripts/dev-db.sh use 设置文件 hfile_自己的名字`，Django 会自动建 `test_hfile_自己的名字` 库，跑完再删掉。

```bash
mkdir -p /tmp/hfile-test
python -B manage.py check --config /tmp/hfile-test/config.toml   # 新建一个设置文件，默认就是 SQLite
# 在里面填上 [site] secret_key（至少 32 个字符），然后：
HFILE_CONFIG=/tmp/hfile-test/config.toml python -B manage.py test
```

SQLite 上可以加 `--parallel` 并行跑，快得多。要有 `requirements-dev.txt` 里的 tblib：没有它，一个测试失败就会让整次测试中断。失败的子测试由 worker 进程按名字交给主进程（`apps/core/testing.py` 的 `_SentSubTest`）：Django 本来把子测试连同它的测试一起 pickle 过去，测试留着 pickle 不了的东西（驱动和它的连接、测试里生成的类）时，报出来的是 PicklingError，看不到真正的失败。MySQL 上并行跑还要有 `mysqldump` 和 `mysql` 命令（Django 用它们复制测试库），没有时就串行跑。

推到 main 和每个 Pull Request 都由 CI（`.github/workflows/tests.yml`）在 GitHub 的 Ubuntu 24.04 上跑一遍全部测试：SQLite 并行，MySQL 8.4 串行，用的是 `scripts/dev-db.sh up` 起的同一个容器，x86-64 和 arm64 的机器上各跑一遍（发布时两种都打包），Windows（Windows Server 2025 的 x86-64、Windows 11 的 arm64）和 macOS 15（Apple 芯片、Intel）上用 SQLite 并行各跑一遍（这些运行器起不了 MySQL 的容器）；浏览器测试单独一路，只在 x86-64 上跑，用运行器自带的 Google Chrome，不许跳过，失败时把页面截图作为 artifact 上传。环境照上面的步骤从头建（`environment.yml`，再装 `requirements.lock`；Windows arm64 和 Intel 的 Mac 照上面说的），和 `requirements.lock` 对不上就停下（`scripts/lock.py --check`）；还用 `makemigrations --check` 检查有没有漏写的迁移。

两种数据库上全部测试都通过。只有一种数据库才有的测试在另一种上跳过，比如只在 MySQL 上的命名锁、备份再恢复每种列类型、恢复的白名单、恢复到一半失败以后换回原样，和安装向导对 MySQL 服务器的检查；只在 SQLite 上的锁文件、备份再恢复和恢复的白名单。SQLite 上还跳过内存里的测试库做不到的几个。两种数据库上都跳过的，是只在 Windows 上跑的、驱动没有的能力（比如不能复制）对应的契约测试，和 moto 检查不了的。发布前做的检查也写成了测试：`*/test_release_*.py`、`*/tests/test_release_check.py`，以及 `apps/crosscheck/tests/`（访问规则的跨应用测试：对访客、普通用户、分享的访客和管理员，经过每个带存储源和路径的接口，试根目录、过滤规则、目录密码、签名链接、内联回答和符号链接）。其中三个标着 `expectedFailure`，是决定不改的限制（注释写着「Accepted known limit」和原因）；哪天它们通过了，测试会报「意外通过」而失败，那时去掉标记。`apps/storages/tests/` 里的驱动测试是普通的 unittest，不用测试数据库，也一起跑；它们的消息要按 Django 的设置翻译，所以单独跑也要用 `manage.py test`。

- 在 Windows 上跑测试：环境照上面的步骤用 Miniforge 建；测试要建符号链接，所以要以管理员身份运行，或者打开 Windows 的开发者模式。Windows 上跳过的测试多一些：只有 Linux 才有的（权限位、命名管道、SIGKILL、`PR_SET_PDEATHSIG`、发布模式的检查），和用到 Windows 不允许的文件名的。浏览器测试在 Windows 上也能跑：没装 Chrome 时，`HFILE_TEST_CHROME` 指向系统自带的 Edge（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，也是 Chromium）；其中两个在 Windows 上跳过：名字里有 `\` 的，和 100 层文件夹、路径超过 260 个字符的。仓库的 `.gitattributes` 让 Windows 上检出的文件也用 LF 换行，和提交的一样。Windows 上 `--parallel` 的 worker 进程是新启动的（spawn），不像 Linux 上从主进程分出来（fork），所以没有测试运行器只在主进程里做的设置：这类设置（比如测试进程里的密码用 MD5）要在 `apps/core/testing.py` 的 `_set_up_worker` 里给 worker 再做一遍。测试打开的文件要关掉，改成只读的要改回来：Windows 删不掉开着的和只读的文件，`shutil.rmtree(..., ignore_errors=True)` 又不报错，测试的临时文件夹就留在了 %TEMP% 里。没关的 sqlite3 连接要等垃圾回收才关；测试客户端拿到的文件下载（`FileResponse`）要把正文读完才关。Windows 上 Python 3.12 的 `time.monotonic()` 是 `GetTickCount64()`，15.6 毫秒才走一格，挨着的两次常常一样：比较时间的断言要留出浮点误差。
- 在 macOS 上跑测试：环境照上面的步骤建。跳过的测试比 Linux 上多几个：名字不是 UTF-8 的文件（APFS 不允许这样的名字）、进入一个不能读的文件夹（要 Linux 的 `O_PATH`）、外面的进程被强行结束时网站跟着停下（`PR_SET_PDEATHSIG`）。macOS 的文件系统默认不分大小写，和 Windows 一样；`--parallel` 的 worker 进程也和 Windows 上一样是新启动的（spawn）。
- 测试之间不共用频率限制的计数：每个测试开始前清空缓存（`apps/core/testing.py`）。测试进程里的密码用 MD5 哈希（Django 默认的 PBKDF2 一个要 0.3 秒）；测试另外启动的程序照常用 PBKDF2。
- 单点登录的测试（`apps/sso/tests/`）用 `apps/sso/tests/fake_idp.py`：在 127.0.0.1 的随机端口上起一个假的 OAuth 2 / OpenID Connect 服务商，它检查 PKCE、Client 的密钥（表单或 HTTP Basic，只许一种）和 redirect_uri，还能按测试的要求回答错误的状态码、太大的回答、跳转、压缩的或者不是 JSON 的回答、很慢的回答。手动试单点登录时可以单独运行它：`python -m apps.sso.tests.fake_idp --port 18351`（不依赖 Django），在管理页新增「通用 OpenID Connect」，.well-known 地址填 `http://127.0.0.1:18351/.well-known/openid-configuration`，Client ID 和 Secret 是 `hfile-client` 和 `hfile-secret`。
- `config/test_serve.py` 真的启动 `hfile.py`：安装、重启、反向代理后面安装、端口被占用、SIGINT 只发给外层、`kill -9` 外层、SIGTERM 时手上的请求和还在排队等线程的请求照样回答，终端里按 Ctrl+C（整个进程组收到 SIGINT）时也一样。`apps/installer/test_connections.py` 在测试进程里起一个 waitress，把期限改成 1 秒，试发得太慢的请求头和内容、一个地址开太多连接；分块的请求体和 waitress 自己的 `ChunkedReceiver` 对照（固定的种子生成几千个对的和错的请求体，每个按各种方式切成几次读到，每次读完消耗的字节数、错误、内容和 trailer 都要和 waitress 一样；块大小行末尾带 LF 的例外，那些要和把 `match` 换成 `fullmatch` 的 waitress 逐次一样，以 waitress 自己的错误结束），三条上限各试刚好不超过和超过一点，还数每次读到的数据被拷贝和查找了多少字节（不计时间）；waitress 换了版本时有一个测试会失败，提醒重新对照。
- 升级的测试（`apps/upgrades/tests/`）会真的改表：用 `MIGRATION_MODULES` 给 siteconf 换上测试用的迁移（`migrations_ok` 加一张表、改一行数据；`migrations_failing` 的第三个迁移改到一半故意失败），检查升级、失败后恢复、数据库比程序新、别人的库。MySQL 不能回滚这些操作，所以它们是 `TransactionTestCase`，每个测试结束时把测试迁移退回去。
- 浏览器测试（带 `browser` 标签）：`apps/web/test_browser.py` 的 `BrowserTestCase` 用 Playwright 驱动系统装的 Google Chrome（`HFILE_TEST_CHROME` 可以指定别的），打开 `StaticLiveServerTestCase` 在测试进程里起的网站，看页面上的脚本做了什么。每个页面记下出了什么错：没接住的异常、控制台里的错误（脚本的、CSP 拦下的、没加载出来的文件）、失败的请求、网站 400 以上的回答，`assertClean()` 检查它们。登录不走登录页，直接放进会话的 Cookie；每个测试的浏览器配置是新的，固定窗口大小、语言、主题和时区，关掉动画。现在有：每个页面（登录页、首页、存储源和文件夹、我的分享、管理页的每一节、错误页、调试模式的 `/components/`）在中文浅色和英文深色下各打开一次，预览自测页 `/components/preview/selftest/`（`apps/preview/tests/test_browser.py`），检查本身能不能发现这些错。各个应用的 `test_browser.py` 里还有核心流程：用密码登录和退出、两步验证登录，文件列表里新建文件夹、重命名、移动、复制、删除、上传、预览和下载，创建带密码的分享、访客打开它，生成直链和短链再打开，管理页的向导添加本地存储源，以及新建用户、给这个用户存储源权限、用新账号登录后看到文件；以及修在 JS 里的几个问题的回归测试：上传面板给要建的文件夹分批、名字里的 `\`，在线编辑带 NUL 的文本，二维码装不下时的两步验证，清除单点登录的 Client Secret，规则测试里用户的根目录的错误。把这几处改回修复以前的样子，对应的测试都会失败。没装 Playwright（`requirements-dev.txt`）或者没有 Chrome 时跳过，设了 `HFILE_BROWSER_TESTS=required` 时算错误；`--exclude-tag browser` 不跑它们，`--tag browser` 只跑它们；设了 `HFILE_BROWSER_ARTIFACTS` 时，失败的测试把页面截图存进这个文件夹。测试用的网站一次只回答一个请求：SQLite 的测试库在内存里，只有一个连接，网站的几个线程同时用它会读错（页面同时要几样东西时，会话读到一半，回答 401 或 500）。

## 添加存储源

每种存储源是 `apps/storages/drivers/` 里的一个驱动类，继承 `base.py` 的 `StorageDriver`。驱动只管和后端打交道：网站用干净的存储路径调用它（`/` 是存储源的根，见 `base.py` 开头的说明），用户、权限、过滤规则、目录密码和链接都由网站管。驱动只用标准库和 `requirements.txt` 里的包。

`apps/storages/drivers/example.py` 是一个写全了的示例，新写驱动时从复制它开始。它对着一个虚构的「示例网盘」，接口由测试用的假服务器 `apps/storages/tests/fake_example.py` 定义（那里开头的说明就当作服务的官方文档）。示例里有基路径、令牌、翻页、限流以后再试、把错误换成异常、按范围读、下载链接、流式上传、新建文件夹、删除、重命名、移动和复制，测试在 `apps/storages/tests/test_driver_example.py`。它不在 `DRIVERS` 里，新建存储源时选不到。

### 要加的文件

- `apps/storages/drivers/<模块>.py`：驱动。
- `apps/storages/tests/fake_<模块>.py`：后端的假服务器，用 `fake_cloud_http.py` 的 `FakeServer` 在本机的随机端口上回答，数据放在内存里。它照官方文档实现驱动用到的接口：登录和令牌、翻页、错误码、上传的每一步。真的服务拒绝的它也要拒绝，比如签名不对、少了请求头、令牌过期、名字被占，这样驱动写错了测试才会失败。不是 HTTP 的后端在本机起一个真的服务器（FTP 用 pyftpdlib，SFTP 用 paramiko，WebDAV 用 WsgiDAV），或者用模拟它的库（S3 用 moto，SMB 用 `fake_smb.py`）。
- `apps/storages/tests/test_driver_<模块>.py`：测试，见下面的「测试」。

### 要登记的地方

1. `apps/storages/drivers/__init__.py`：
   - `DRIVERS` 里加上 `"类型": "apps.storages.drivers.<模块>:<类>"`，新建时的下拉框按这个顺序列出；
   - `GROUPS` 里放进一组：本地和服务器、对象存储、网盘、自建服务、分享链接和发布页。

   类型用小写字母、数字和 `-`，和驱动类的 `type` 一样；它存进数据库，以后不能改。
2. `apps/storages/static/storages/js/manage_storages.js`：
   - `TYPE_ICONS` 是管理页上这种存储源的图标，不写就是 `storage`；
   - 新的辅助按钮在 `HELPER_ICONS` 里给图标。

   只能用 `scripts/icons.py` 里有的图标，要新的先加进那里，再重新生成。
3. `apps/files/transfer.py`：
   - `download_url()` 每个文件要问一次后端的，加进 `DEFERRED`：列表里先给本站的签名链接，打开时才去问；
   - `download_url()` 在本地算签名的，加进 `LOCAL_URLS`：页面的 CSP 按它给的地址放行；链接会跳到别的主机的（ImgBed 的外链渠道）不加；
   - 链接只对要它的那个 User-Agent 有效的，加进 `UA_BOUND`。
4. 翻译：先 `python scripts/i18n.py extract`，填好中文，再 `python scripts/i18n.py compile`，见[翻译](#翻译)。
5. 文档：
   - [功能说明](features.md)的「存储源」一节写清楚：
     - 要填什么，凭据去哪里拿；
     - 能做什么、不能做什么；
     - 照什么写的，有没有用真的服务试过。
   - `README.md` 和 `docs/README.en.md` 里的类型列表和数量。
   - `CHANGELOG.md` 里下一个版本的那一节。

### 驱动的写法

现有的驱动都照 `example.py` 的样子写，读过一个，在别的里面也知道去哪里找。`apps/storages/tests/test_layout.py` 检查 `DRIVERS` 里的每个驱动和 `example.py` 是不是照下面的「开头的说明」「文件里的顺序」「类里的顺序」写的；这几条规则要改时，测试跟着改。

**开头的说明**

第一段说后端是什么，给出官方文档的地址，和照着写的版本。后面按话题分节，每节一行标题，内容缩进四格。标题用下面这几个，按这个顺序，用不上的不写：

- `Paths`：存储路径怎么对应到后端：基路径，按路径还是按 id 找，文件夹怎么列、怎么翻页，缓存什么。
- `Login`：凭据和令牌：怎么取得、刷新和保存。
- `Requests`：
  - 请求怎么发、怎么签名；
  - 回答和错误是什么样子；
  - 怎么重试、怎么限流。
- `Changes`：新建文件夹、删除、重命名、移动、复制各用哪个接口，会不会覆盖，删掉的进不进回收站。
- `Downloads`：`open()` 和 `download_url()`。
- `Uploads`：`write()` 和 `upload_target()`。
- `Names`：后端不收的字符，名字的长度，分不分大小写。
- `Limits`：做不到的，没有核实的，有没有用真的服务试过。

别的话题自己起一个标题，放在和它挨着的那一节后面，比如 S3 的寻址方式、本地存储的符号链接。

**文件里的顺序**

1. 开头的说明。
2. 导入：最前面是 `from __future__ import annotations`，然后分四组：标准库、第三方库、Django、本项目。本项目的写成：
   - `from . import http, paths`；
   - `from ._cloud import 名字`；
   - `from .base import 名字`。
3. 常量，比如消息里的服务名 `SERVICE`、接口地址的固定部分。
4. 模块里的辅助函数和小类；多的时候用 `# --- 话题 ---` 分组。
5. 驱动类。
6. 驱动类的子类，比如 S3 的各家、OneDrive 的世纪互联版。

**类里的顺序**

1. `type`、`label`、`params`。
   - `label` 用 `gettext_lazy()`；不翻译的名字（`"WebDAV"`）直接写。
   - 按 id 寻址的驱动还有 `service`（消息里的服务名）和 `root_id`，写在 `label` 后面。
2. 能力开关。`can_range`、`can_direct_download`、`can_direct_upload`、`can_move`、`can_copy`、`can_search`、`has_folders` 七项都写出来，和默认值一样的也写，一眼就看得出它能做什么；子类只写和父类不同的。
   - 只读的驱动再写 `can_write = False`，不能上传的写 `can_upload = False`。
   - `download_url()` 给的链接可能只能用一次的，写 `reusable_links = False`：本站每次打开都去要，不把几分钟前拿到的再给出去。
   - 按 id 寻址的驱动接着写 `upload_conflict`。
   - 后端对名字有规定时写 `forbidden_chars` 和 `max_name_bytes`。
3. 表单要的：`oauth`、`helpers`、`setup_params`、`auth_params`。
4. 写成 `ClassVar` 的接口地址和调节用的数（每页几项、有效期、分块大小），前面一行注释说明它们是什么：测试把地址指向假服务器，把数改小。
5. `__init__()` 在这里把设置读一次；`close()` 紧跟在后面。
6. 方法分成几节，每节用一行 `# -- 节名` 开头：
   - `# -- the admin form`：`validate_config()`、`authorize_url()`、`exchange_code()`、`helper()`。
   - `# -- login`：登录，取得和刷新令牌。
   - `# -- requests`：发请求，把错误的回答换成异常（`_error()`），把后端的回答换成 `Entry`。
   - 后端特有的部分，自己起一节，比如 `# -- walking paths safely`。
   - `# -- files`：`base.py` 里的那些方法，按那里的顺序：`check`、`list`、`stat`、`open`、`open_known`、`download_url`、`upload_target`、`write`、`mkdir`、`delete`、`rename`、`move`、`copy`、`search`。
     - 大的驱动分成 `# -- reading` 和 `# -- writing` 两节。`# -- reading` 是 `check` 到 `download_url`，加上 `search`；`# -- writing` 是 `upload_target` 到 `copy`。
   - `# -- IdDriver's hooks`：按 id 寻址的驱动的钩子，按 `_ids.py` 里的顺序。

   在这几节（`files`、`reading`、`writing`、`IdDriver's hooks`）里，只给一个方法用的辅助方法放在它的前面；几个方法共用的，放在第一个用它的方法前面。

**参数**

- `Param` 的 `label` 和 `help` 用 `gettext_lazy("英文")`。`help` 说清楚值去哪里拿：服务的哪个页面，应用的哪项设置。
- 凭据标 `secret=True`：令牌、密码、Cookie、密钥，和跟令牌一起保存的有效期。这样的参数加密保存，表单上不再显示。
- 存储源的根是后端里的一个文件夹时，参数叫 `base_path`（`kind="path"`）。
- 几个参数之间的规则和格式写在 `validate_config()` 里，回答 `{参数: 消息}`。

**出错**

- 抛 `base.py` 里的异常，网站按种类回答（404、409、502……）：
  - 找不到：`NotFound`；
  - 名字被占：`AlreadyExists`；
  - 不是文件夹：`NotADirectory`；
  - 凭据不对或者过期了：`AuthError`；
  - 后端出错、限流、连不上：`BackendError`；
  - 不支持：`Unsupported`；
  - 别的：`DriverError`。
- 把后端的错误码对应到这些异常上，消息里说清楚该怎么办。
- 别的异常都算程序的错误：回答 500，记下 traceback。
- httpx 的错误用 `_cloud.transport_error()` 换成 `BackendError`。它不带请求的地址，因为地址里可能有令牌。

**消息**

- 代码里只写英文。函数里用 `gettext()`，类和模块里用 `gettext_lazy()`。
- 带参数的写成 `gettext("… %(name)s …") % {...}`：`%` 用在 `gettext()` 的结果上，不用在惰性的字符串上。
- 代码里不能有中文，`apps/web/test_i18n.py` 会查。后端的回答里要比对的中文是例外。
- 常见的消息先找现成的：`base.py`、`_cloud.py` 和别的驱动里用的。意思一样，就用一样的原文。

**线程**

- 一个驱动实例同时给很多请求用，每个方法都要线程安全。
- 一个 httpx 的 Client 可以给几个线程一起用。会变的东西（令牌、缓存）放在锁里，或者放进 `_cloud` 的 `TokenCache`、`TTLCache`。
- 一个连接一次只能做一件事的（FTP、SFTP），用连接池 `pool.py`。

**发请求**

- HTTP 客户端只用 `http.client()` 建：它用系统的 CA 证书，有超时和 HFile 的 User-Agent。
- 它默认不跟随跳转。要跟随时，不能把凭据带到别的主机。
- 要去管理员填的地址、又要跟随跳转的驱动（目录列表、URL 树、ImgBed）用 `http.client(guard=True)`，每个请求带 `extensions=http.configured(填的地址)`：填的地址在互联网上时，跳转和名字都不能把请求引到本站所在的网络里，拒绝时是 `http.InsideNetwork`（一个 `DriverError`），什么都不发过去；填的地址本身在局域网里时不限制。
- 不要正文的回答用 `http.drain()` 关掉，错误的、写操作的、HEAD 的都是：短的读完，连接还能再用；长的不读。流式的回答没读到结尾就 `close()`，httpx 连连接一起关掉，没有正文的 204 和 HEAD 也是这样。已经读过的回答，`drain()` 只关。
- 流式请求的错误回答，要看里面说了什么（错误的 JSON）的，用 `http.read_error()` 读：只读前 64 KiB，最多 10 秒，返回一个读好的回答，`json()`、`text` 照常用。不要 `response.read()`：服务器或者中间的代理回多长，就读多少进内存。

**下载**

- `open()` 按 offset 和 length 读。后端支持 Range 就用 Range，用 `_cloud.range_header()` 和 `_cloud.response_stream()`，请求时带 `Accept-Encoding: identity`。
- `open()` 要先问后端这是什么文件时（WebDAV 的 PROPFIND），再重写 `open_known()`。网站按范围下载之前刚 `stat()` 过，会把那个 `Entry` 交给它，省掉一次请求。
- `download_url()` 只在浏览器拿着链接就能下载时给出链接。要带密钥的请求头、Cookie、特别的 User-Agent 或 Referer 时回答 `None`，网站就经过 `open()` 转发。
- 链接多久过期由后端定（不管 `expires`）、后端又说了的，再重写 `timed_download_url()`，一起给出这个链接还能再给出去几秒（Cloudreve v4）：本站缓存它的时间不超过这个，也不超过链接有效期的一半。
- `download_url()` 可以给 http:// 的链接：网站是 https 时，`transfer.insecure_for()` 把它换成本站的 /pd/ 链接（https 页面里的 http 地址是混合内容）。

**上传**

- `write(path, data, size)` 从 `data` 边读边发，不把整个文件读进内存。`_cloud.exact_body()` 正好发出 `size` 个字节，`data` 不够时报错。
- 后端要先给整个文件的哈希，`data` 又不能 `seek` 时，先存进临时文件（open115 的 `_Source`）。
- 上级文件夹不存在时要建出来。同名的文件就替换掉；同名的是文件夹时报 `AlreadyExists`。
- 照后端的分块大小和上限来。
- 浏览器直传用 `upload_target()`，后端要允许本站的来源（CORS）。

**只读的后端**（分享链接、发布页、目录列表）

- `can_write = False`，`can_move` 和 `can_copy` 也是 `False`。
- 不写 `write()`、`mkdir()`、`delete()`、`rename()`。
- 测试用 `ReadOnlyContract`。

**按 id 寻址的后端**（多数网盘）

- 继承 `_ids.IdDriver`，只实现它说明里列出的钩子：`_fetch`、`_create_node`、`_delete_node`、`_rename_node`、`_move_node`、`_copy_node`、`_upload_node`、`_read_node`，和根目录的 `root_id` 或 `_account_root()`。路径、缓存、防止重名、上传时安全地替换都由它做。
- 最小的例子是 `apps/storages/tests/test_ids.py` 里的 `MemoryDriver`。
- 别的方法和属性不要和它内部的重名（`_ids.py` 里用到的名字）。要改它的方法时，调用 `super()`。

**登录**

- OAuth 授权码：
  - `oauth = True`，写 `authorize_url()` 和 `exchange_code()`；
  - 回调地址是本站的 `/storages/oauth/callback/`；
  - 管理员填自己建的应用的 ID 和密钥。
- 扫码：
  - 写一个 `helpers` 按钮（`"qrcode"`），回答二维码的图片和 `poll`；
  - 网站带着 `args` 每隔几秒再问一次，登录好了回答 `{"changes": {...}}`；
  - 见 `base.py` 的 `helper()` 和 `open115.py`。
- 驱动自己刷新令牌时：
  - 用 `update_config({...})` 交给网站加密保存；
  - 一起用的几项（访问令牌、它的有效期、刷新令牌）在 `self.token_lock` 里读和改。网站给同一个存储源的几个实例同一把锁，不会有两个实例拿同一个只能用一次的刷新令牌去刷新（见 `baidu_netdisk.py`）。
- 只用管理员自己的开发者应用或账号，不用别的客户端里内置的凭据。

### 测试

`test_driver_<模块>.py` 照 `test_driver_example.py` 写：

- 一个 mixin 在 `setUpClass` 里起假服务器、在 `tearDownClass` 里关掉，并给出指向它的驱动。要改小的数（每页几项）用驱动的子类改。
- 契约测试（`apps/storages/tests/contract.py`）：
  - 读写的驱动混入 `DriverContract`，只读的混入 `ReadOnlyContract`，后者要假服务器里有 `READ_ONLY_TREE` 和 `EMPTY_FOLDER`；
  - 实现 `make_driver()`，回答一个根目录是空文件夹的驱动；
  - 有基路径的，再用一个基路径跑一遍；
  - 驱动没有的能力（`can_range`、`can_move`、`can_copy`、`can_direct_download`、`has_folders`）按它的能力开关跳过。
- 契约以外，再测这种后端特有的：
  - 令牌的刷新和保存（`on_update`）；
  - 翻页、错误码、限流；
  - 每一种上传的做法：秒传、一次传完、分块；
  - 登录用的辅助按钮；
  - `validate_config()`；
  - `download_url()`。
- 不比对消息的原文，翻译以后就不一样了。比对异常的种类，和消息里的值：路径、错误码、名字。

```bash
HFILE_CONFIG=/tmp/hfile-test/config.toml python -B manage.py test apps.storages.tests.test_driver_example
```

驱动的测试也要用 `manage.py test` 跑：消息要按 Django 的设置翻译，直接 `python -m unittest` 会出错。

## 截图

README 里的截图（`docs/images/` 里中文界面的 `*.png`、英文界面的 `*.en.png`）用 `scripts/screenshots/run.sh` 重拍：

```bash
conda activate hfile
scripts/screenshots/run.sh            # 写进 docs/images/，也可以给一个别的文件夹
```

它每次新建一个演示站点：`make_content.py` 生成的演示文件（图片是画出来的，修改时间是固定的）放在一个本地存储源里，其中的照片、软件两个文件夹另外各是一个存储源，三个都显示在左侧栏里，各有图标；管理员登录，再用 Playwright 驱动系统的 Chrome（`$CHROME`，默认 `/usr/bin/google-chrome`），中文、英文界面各截五张。Playwright 装在工作文件夹的 venv 里（venv 里没有 `run.sh` 写的那个版本时，比如第一次运行、上次没装完，就重建 venv 再 `pip install`；也可以用 `PLAYWRIGHT_PYTHON` 指定一个装好了它的 Python），工作文件夹默认是 `$TMPDIR/hfile-screenshots`。演示站点用的是工作区里的代码。在同一台机器上重拍，界面没变时截图也不变，只有画廊里图片缩放的细微差别（肉眼看不出），所以 git 里看到的变化就是界面的变化。
