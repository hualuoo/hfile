# 接口

[HFile](../README.md) › [文档](README.md)

都在 `/api/v1/` 下，完整列表在 `config/api_urls.py` 开头。

- 请求和回答都是 JSON，字符集只收 UTF-8。例外：网站图标和 Logo 的上传用 multipart，上传文件的块（`PUT /api/v1/uploads/<id>/`）请求体就是文件的字节，短链的「导出」回答 CSV。
- 出错时回答 `{"detail": "…"}`，或者按字段 `{"username": ["…"]}`，用页面的语言：Cookie `hfile_language`（顶栏的语言菜单选的），没有就按 `Accept-Language`，中文或英文（回答头 `Content-Language` 写着是哪种）。JSON 写错时说明在第几行第几列；`NaN`、`Infinity` 和超出浮点数范围的数（比如 `1e400`）不收；一个请求里的对象和数组最多 50000 个。
- 时间都是网站时区的 ISO 8601，精确到秒，比如 `2026-11-13T10:03:04+08:00`（文件的修改时间早于 1901 年时用 UTC）。
- 文字字段只收 JSON 的字符串：发数字（比如 `98765432.10`）、true、列表都回答 400「应该是文字（JSON 里的字符串）。」，不会被悄悄换成别的写法再存下。
- 不回答 OPTIONS（405）。
- 登录用 Cookie 会话。所有写操作都要带 `X-CSRFToken` 请求头，登录也要带；令牌从 `GET /api/v1/session/` 拿。CSRF 不对时回答 403，带 `"code": "csrf_failed"`。
- 列表按 `?page=` 和 `?page_size=` 分页（每页默认 20 条），回答里的 `next` 是完整网址，页面只取它的路径。
- 没说可以不登录的接口都要先登录。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/session/` | 登录状态 `{"authenticated", "user", "step", "csrf_token"}`；`step` 是一个还差几步的登录正在等的那一步，没有是 null |
| POST | `/api/v1/session/` | 登录 `{"username", "password"}`，用户名不分大小写。登录了回答 201 `{"authenticated": true, "user": {…}}`；还要别的步骤时回答 200 `{"authenticated": false, "step": "…", "before_password": …, "detail": "…"}`，页面显示这一步，再 POST `{"step": "…", 这一步的字段}`（`before_password` 为 true 时连同用户名和密码一起再发一次）。步骤有 `captcha`（图片验证码，在密码之前：`captcha_key` 和 `captcha`）和 `totp`（两步验证，在密码之后：`code`，一次登录最多答错 5 次）。设了登录安全入口时每个 POST 都要带 `entry`，不带或不对回答 404。每个 IP 每分钟最多 10 次（IPv6 按 /64 算），已经登录了也这样算 |
| DELETE | `/api/v1/session/` | 退出，对谁都回答 204 |
| PUT | `/api/v1/users/me/password/` | 改密码 `{"old_password", "new_password"}`，回答 204。这个会话保持登录，其他会话失效。没有密码的账号（单点登录建的）设第一个密码时不用 `old_password`。每个用户每小时最多 10 次 |
| GET | `/api/v1/users/me/totp/` | 自己的两步验证 `{"enabled", "enabled_at", "available"}`（`available`：超级管理员，或者已经开着） |
| POST | `/api/v1/users/me/totp/setup/` | 超级管理员：生成新的密钥 `{"secret", "uri", "qr"}`（`qr` 是二维码的 SVG data: 地址；站点名称和用户名太长、二维码装不下时是空的，只能手动输入密钥），在会话里等 10 分钟 |
| POST | `/api/v1/users/me/totp/` | `{"code", "password"}`：手机上的 6 位数字对了才开启，密钥加密保存，以后不再发回 |
| POST | `/api/v1/users/me/totp/disable/` | `{"password"}`：关掉。这三个每人每小时最多 20 次 |
| POST | `/api/v1/captcha/` | 不用登录：`{"scene": "login"}` 取一张验证码图片 `{"required": true, "key", "image", "width", "height", "length", "expires_in"}`，不要验证码时 `{"required": false}`。每个 IP 每分钟最多 30 张，Django 后台的登录页每显示一次也算一张 |
| GET | `/api/v1/manage/users/` | 管理员：用户列表，`?search=`（用户名、昵称或 ID）、`?is_active=true/false`、`?kind=normal/guest/template`、`?ordering=`（`id`、`username`、`date_joined`、`last_login`，前面加 `-` 倒序）。每个用户带 `can` 和 `why`：这个管理员能对他做什么、不能的原因；`last_login_location` 是最近登录 IP 的归属地（按页面的语言，比如 `"中国 浙江 杭州"`，英文是 `"Hangzhou, Zhejiang, China"`；没启用 IP 归属地或者不知道时是 `""`） |
| POST | `/api/v1/manage/users/` | 管理员：新建用户 `{"username", "nickname", "password", "is_active", "default_permissions"}`，回答 201。密码空着就是不能用密码登录；`default_permissions` 里缺前置的权限回答 400（比如「「获取直链」要先有「下载」的权限。」，前置见 `grant-options`） |
| GET / PATCH / DELETE | `/api/v1/manage/users/{id}/` | 管理员：一个用户；改（字段同上，都可以不写，`"password": ""` 表示不改）；删除（204） |
| POST | `/api/v1/manage/users/{id}/copy/` | 管理员：复制成新用户 `{"username", "nickname", "password"}`，回答 201 |
| GET | `/api/v1/manage/activity/` | 管理员：操作日志，新的在前。`?search=`（用户名、昵称、存储源名称、路径、Key，或者 IP 的开头）、`?kind=`（一种操作，比如 `login_failed`、`delete`、`update_storage`）或 `?category=`（`account`、`files`、`links`、`users`、`storages`、`site`）、`?storage=`（存储源 ID）、`?user=`（用户 ID：这个用户做的和关于这个用户的）、`?ip=`（一个地址）、`?since=`、`?until=`（YYYY-MM-DD，都含当天）。每条有 `kind` 和 `kind_label`、`category`、`actor`（谁做的：`{"id", "username", "nickname", "kind"}`，账号删掉以后 `id` 是 `null`；没登录的登录失败和服务器上的命令是 `null`；没登录的访客是内置的匿名用户）、`target`（关于哪个用户：管理员改的人、登录失败时的账号或者输入的用户名）、`storage`（`{"id", "name", "key"}`）、`path` 和 `to_path`（文件或文件夹在存储源里的完整路径，以及改名、移动、复制以后的）、`key`（分享或短链的 Key）、`detail`（存下的代码和数值）和 `detail_text`（按页面语言写出来的详情）、`page`（相关的管理页 `{"section", "label"}`）、`ip`、`ip_location`（规则同用户列表的 `last_login_location`）、`user_agent`。只有 GET：没有修改和删除的接口 |
| GET | `/api/v1/sso/providers/` | 不用登录：登录框下面的单点登录按钮，启用的服务商按顺序 `[{"code", "name", "icon"}]` |
| GET | `/api/v1/sso/links/` | 自己的「账号绑定」：`{"has_password", "providers": [{"code", "name", "icon", "enabled", "linked", "external_id", "created_at", "last_login_at"}]}`（启用的服务商，和自己绑定过的） |
| POST | `/api/v1/sso/links/{code}/` | 绑定 `{"password", "next"}`：有密码的账号要输登录密码（和改密码共用每小时 10 次）；回答 `{"url"}`，页面打开这个服务商的地址，服务商那边同意以后回到 `next` 并带上 `?sso_linked=简称`。已经绑定了这个服务商时回答 409 |
| DELETE | `/api/v1/sso/links/{code}/` | 解除绑定，回答 204；这是唯一的登录方式（没有密码、也没有别的绑定）时回答 409 |
| GET / POST | `/api/v1/manage/sso/providers/` | 管理员：全部服务商（不分页，按顺序）；超级管理员：新建一个 `{"code", "name", "icon", "client_id", "client_secret", "auth_url", "token_url", "userinfo_url", "scope", "token_auth", "id_field", "username_field", "nickname_field", "enabled", "order"}`，回答 201。`client_secret` 只写不读，回答里是 `client_secret_set` 和 `client_secret_unreadable`（`secret_key` 换过、读不出来了），还有 `callback_url`（要在服务商那里登记的回调地址）和 `identities`（绑定了几个用户）。有错时按字段回答 |
| GET / PATCH / DELETE | `/api/v1/manage/sso/providers/{id}/` | 管理员：一个服务商；超级管理员：改（字段同上，`client_secret` 空着或不发就不变，写 `null` 就清除，改成公开客户端）、删除（绑定的账号一起删掉，回答 204） |
| POST | `/api/v1/manage/sso/providers/order/` | 超级管理员：`{"ids": [...]}` 全部服务商的 ID，按登录页上的新顺序，回答新的列表 |
| POST | `/api/v1/manage/sso/discover/` | 超级管理员：`{"url"}` OpenID Connect 的配置地址（`.well-known/openid-configuration`），由服务器去读，回答 `{"auth_url", "token_url", "userinfo_url", "scopes", "token_auth", "issuer"}`（读不到的是 null）；读不了时 400 `{"url": ["…"]}` |
| GET / POST | `/api/v1/manage/users/{id}/identities/` | 管理员：这个用户绑定的单点登录账号 `[{"id", "provider": {…}, "external_id", "created_at", "last_login_at"}]`；`{"provider": 服务商 ID, "external_id"}` 替他绑定一个，回答 201。内置用户不能绑定；谁能看和改谁照用户管理的规则（其他管理员的只有超级管理员能）；要绑定的账号已经绑在别人身上时，只有能看那个人的管理员才会被告知是谁，别的管理员只看到「这个账号已经绑定了别的用户。」；自己的账号在这里只能看，绑定和解除用「账号绑定」 |
| DELETE | `/api/v1/manage/users/{id}/identities/{绑定的 ID}/` | 管理员：解除这个绑定，回答 204（规则同上） |
| GET | `/api/v1/site/` | 网站公开的设置，不用登录：站点名称、Logo 和它的链接、备案号、页面布局和文件列表的习惯、各类预览实际用的后缀（`preview_suffixes`，关掉的是空列表）、OnlyOffice 和 kkFileView 的地址、直链和短链的开关、直链前缀、短链有效期、是否显示登录按钮（`show_login`，设了登录安全入口时总是 false）。不含任何密钥和内部的设置（Secret、登录入口、黑名单、防盗链规则、自定义代码） |
| GET | `/api/v1/manage/settings/<group>/` | 管理员：一页设置，`group` 是 `site`、`view`、`link`、`proxy`、`security`、`access`（`proxy` 是代理下载：单个 IP 的同时连接数 `proxy_ip_connections` 和总速度 `proxy_ip_speed`，单位 KB/s，0 是不限）。`view` 里的 `onlyoffice_secret` 只写不读，回答里只有 `onlyoffice_secret_set`；后缀列表回答的是实际用的（空着时是默认的）；`access` 还回答网站看到的你的 IP 和 UA（`your_ip`、`your_user_agent`） |
| PATCH | `/api/v1/manage/settings/<group>/` | 管理员：改这一页的一些设置，没发的不变，回答改好后的整页。有错时 400，按字段 `{"site_name": ["…"]}` 一次全部列出；不是这一页的设置名也是 400（GET 回答里只读的那些可以照样发回来），名单（IP 黑名单、UA 黑名单、Referer 规则）里写错的行只列前 20 行，再说还有几行。`onlyoffice_secret` 发了才改，发 `""` 是删除。`custom_css`、`custom_js`、`announcement`、`guest_home_html`、`onlyoffice_url`、`onlyoffice_secret`、`kkfileview_url` 只有超级管理员能改（见[设计说明](design.md)的「网站的设置」）：别人发来和现在不一样的值是 403，`onlyoffice_secret` 只要发了就是 403（`""` 也是） |
| PUT | `/api/v1/manage/settings/site/<favicon\|home_logo>/` | 管理员：上传网站图标或 Logo（multipart，字段 `file`），检查、转成 PNG 保存，回答整页站点设置；换下来的旧图片删掉 |
| DELETE | `/api/v1/manage/settings/site/<favicon\|home_logo>/` | 管理员：换回 HFile 自己的 |
| POST | `/api/v1/manage/settings/access/test/` | 管理员：`{"ip_blocklist", "ua_blocklist", "ip", "user_agent"}`（都可以不发：不发名单用保存的，不发 IP 和 UA 用自己的），回答 `{"ip": {"value", "rule"}, "user_agent": {"value", "rule"}}`，`rule` 是挡住它的那条规则或 null。什么都不改 |
| GET | `/api/v1/manage/diagnostics/` | 管理员：网站看到的这个请求：`client_ip`、`remote_addr`、`forwarded_for`、`scheme`、`host`、`origin`、`site_url`、`proxies`、`allowed_hosts`、请求头 `headers`（Cookie 只留名字，`Authorization` 和名字里有 token、key、secret 等的值写成 `<已隐藏>`），和看出来的问题 `notes`（`kind` 是 warning、info 或 ok） |
| GET | `/api/v1/manage/version/` | 管理员：程序版本、Django 和 Python 的版本、数据库的种类和版本、`auto_upgrade`、`frozen`（是不是单文件程序）、`backup_dir`，数据库还差的迁移（`pending`）和来自更新版本的迁移（`newer`），以及最近 100 条升级记录（`upgrades`） |
| GET / PATCH | `/api/v1/manage/ip-geo/` | 管理员：IP 归属地。`enabled`、`folder`（`[paths] geoip`）、正在用的数据库 `database`（`file`、`edition`（`city` 城市数据库或 `country` 国家数据库）、`size`、`sha256`、`built_at` 数据日期、`downloaded_at`、`missing` 文件不见了；没下载过是 `null`）、最近一个后台任务 `job`（见下面的 `job/`），以及还没有归属地的记录数 `unfilled`（`{"total", "parts": [{"key": "users"/"activity"/"downloads", "label", "count"}]}`）/ 启用或停用 `{"enabled": true}` |
| POST | `/api/v1/manage/ip-geo/download/` | 管理员：在后台下载（或更新）数据库 `{"edition": "city"}`：`city` 是城市数据库 GeoLite2-City，`country` 是国家数据库 GeoLite2-Country；不带时下正在用的那个，还没有就是城市数据库。下的不是正在用的那个时，下好、检查过以后换成它，原来的文件删掉。回答 202 和上面的状态。`edition` 不对时 400，没启用、或者已经有任务在跑时 409 |
| POST | `/api/v1/manage/ip-geo/fill/` | 管理员：在后台补全归属地 `{"redo": false}`（`true` 时已有归属地的也重查），回答 202。没启用、没有能用的数据库、或者已经有任务在跑时 409 |
| GET | `/api/v1/manage/ip-geo/job/` | 管理员：最近一个后台任务 `{"job": {"id", "kind": "download"/"fill", "kind_label", "state": "running"/"done"/"failed", "done", "total", "message", "started_at", "finished_at"}}`，没有过任务时 `{"job": null}`。`done`/`total` 下载时是字节数，补全时是 IP 个数。3 分钟没有进展的「进行中」算失败 |
| GET | `/api/v1/manage/ip-geo/check/` | 管理员：`?ip=` 的归属地 `{"ip", "code", "location"}`（`code` 是存进记录的代码，比如 `"CN 1784764 1808926"`（国家，再加上省和城市的 GeoNames 编号）、`"US"`、`"PRIVATE"`、`"unknown"`；`location` 是按页面语言的名字）。没启用或没有能用的数据库时 409 |

存储源（管理员；都在 `/api/v1/manage/` 下，`apps/storages/views.py` 开头有完整列表）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `storage-types/` | 每种存储类型（按新建表单里的分组排好：`group`、`group_label`）和它们的参数（名字、说明、类型、必填、默认值、选项、是不是密钥、是不是「更多设置」）、辅助按钮，以及 OAuth 的回调地址 `oauth_redirect_uri`；添加向导用的 `setup`（第二步要填的参数）和 `auth`（第三步怎么授权：`oauth`、`qrcode` 或者空，以及授权填哪些参数）；`superuser` 说看的人是不是超级管理员，每种类型的 `superuser_only`、`superuser_params` 说哪些只有超级管理员能新建、能改（本地存储） |
| GET、POST | `storages/` | 列表（按顺序，带最近一次检查连接的结果 `status`）；新建：`{"key", "name", "type", "enabled", "remark", "config": {参数}, "draft", …}`（下载和上传的选项也在里面，比如 `proxy_connections`：经过本站同时发送几个下载，1 到 1000，默认 16；`proxy_when_full`：满了以后 `link` 转到存储源自己的链接，或者 `refuse` 回答 429，没有自己链接的类型总是存成 `refuse`），先测试连接，连不上回答 400 `{"detail", "code": "check_failed"}`；带 `"setup": true`（添加向导的第二步）时先停用、不测试，后面几步才填的必填参数可以先空着；成功回答 201，`grants` 说有几个用户得到了权限。普通管理员新建本地存储源、改它的文件夹或打开指向外面的符号链接回答 403 `{"detail", "code": "superuser_required"}`（`test/` 也一样）。保存的存储源用不了时（它的类型已经没有了，或者从源码运行时缺驱动要用的库），改设置、启用、检查连接、测试连接、复制都回答 400 `{"detail", "code": "type_unavailable"}`，访客打开它是 403 `storage_disabled` |
| GET、PATCH、DELETE | `storages/<id>/` | 详情（`config` 里没有密钥，`secrets` 说每个密钥是 `set`、`unset` 还是 `unreadable`）；修改（密钥留空不改，`clear_secrets` 列出要清除的，`type` 不能改；改了参数就先测试；还停用着的存储源也可以带 `setup`，向导接着做：后面几步的必填参数可以空着，保持停用）；删除（规则、权限，连同它的短链、下载日志、分享、上传会话和 OnlyOffice 编辑会话一起删） |
| POST | `storages/<id>/enable/`、`disable/`、`check/`、`copy/`、`rotate-link-secret/`、`check-path/` | 启用（先测试）、停用、检查连接、复制 `{"key", "name"}`、换链接密钥、看一个文件夹在不在 `{"path"}` |
| GET、PUT | `storages/<id>/filters/`、`passwords/`、`readmes/` | 一种规则的整张表，按顺序；PUT 整张表替换：带 `id` 的原地修改，没有的新建，表里没有的删除；每种最多 200 条；请求体要是 JSON 列表，表单或别的回答 400，什么也不删 |
| POST | `storages/<id>/rules/test/` | 规则测试 `{"kind", "path", "rules", "root"}` |
| POST | `storages/sort/` | 排序 `{"ids": [每个存储源的 id，按新的顺序]}` |
| GET | `storages/key-check/?key=&id=` | 别名能不能用 |
| POST | `storages/test/`、`storages/helpers/`、`storages/oauth/` | 表单上的「测试连接」、辅助按钮和「授权」：带 `storage`（已有的存储源）或 `type`、`config`、`draft`；添加向导的「授权」还带 `"setup": true`，回调页就回到向导 `#storages/<id>/setup`。服务器给的密钥只放在草稿 `draft` 里，回答只说哪些填好了。辅助按钮还要 `name`（`storage-types` 里 `helpers` 的名字），扫码登录的轮询带上上次回答的 `poll_token` |
| GET、DELETE | `storages/drafts/<id>/` | GET：一份草稿里有什么（不含密钥的值）；DELETE：删掉它（204） |
| GET、PUT | `users/<id>/grants/` | 一个用户在每个存储源上的权限，每个存储源一行：`[{"storage": {"id", "key", "name", "type", "enabled"}, "granted", "enabled", "root_path", "permissions"}]`；PUT 同样的列表，替换这个用户的权限（列表里没有的存储源不变，`granted: false` 删掉这一条；缺前置的权限回答 400）；改别的管理员或超级管理员的，要是超级管理员，不然 403；一次最多 10000 个存储源；请求体要是 JSON 列表 |
| GET | `grant-options/?user=` | 15 种权限：`code`、名字 `label`、说明 `help`、前置 `requires`（要先有其中一个，空列表是没有前置），以及这个用户的默认权限 |

复制任务（管理员；都在 `/api/v1/manage/` 下，`apps/copytasks/views.py` 开头有完整列表）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET、POST | `copy-tasks/` | 任务列表，新的在前，每页 20 个；新建：`{"source", "source_path", "target", "target_path", "threads", "speed_limit", "same_file", "same_name", "retries"}`，回答 201，任务马上在后台开始。`source`、`target` 是存储源 ID（都要启用着，`target` 还要能上传文件）；路径默认 `/`，源文件夹要在；源文件夹复制到 `target_path` 里，成为其中同名的文件夹（`source_path` 是 `/` 时，里面的东西直接复制到 `target_path`），任务的 `target_path` 是复制出来的这个文件夹（从 `/a` 复制到 `/b` 是 `/b/a`），继续时也复制到这里；目标文件夹不在会新建，复制出来的文件夹已经有了就复制进去，它是一个文件或者目标存储源不能用这个名字时回答 400；同一个存储源里，源文件夹和复制出来的文件夹不能是同一个，也不能一个在另一个里面；`threads` 1 到 16（默认 4）；`speed_limit` 是所有线程加起来的字节每秒，0（默认）不限；`same_file` 是目标里有文件名和大小都相同的文件时 `skip`（默认）还是 `overwrite`，`same_name` 是文件名相同、大小不同时（默认 `overwrite`）；`retries` 0 到 10（默认 3）。每个任务有 `id`、`created_at`、`created_by`（`{"id", "username"}`）、`source` 和 `target`（`{"id", "name", "key"}`，存储源删了以后 `id` 是 `null`）、`source_path`、`target_path`、上面的设置、`status`（`running`、`stopping`、`done`、`failed`、`stopped`；运行的进程 60 秒没写进度时算 `failed`）和 `status_label`、`message`（失败或中断的原因）、`run`（第几次运行）、`started_at`、`finished_at`、`seconds`（用时）、`scanning`（还在列出文件，总数还会变）、`files`（`total`、`copied`、`skipped`、`failed`）、`folders_failed`、`bytes`（`total`、`copied`、`skipped`，以及进度 `done`）、`transferred`（从源读了多少字节，读两遍的文件算两遍）、`speed`（最近几秒每秒多少字节）、`average_speed`、`can_stop`、`can_resume` |
| GET | `copy-tasks/<id>/` | 一个任务，比列表多 `current`（正在复制的文件 `[{"path", "size", "done", "speed", "attempt", "error"}]`，`error` 是重试前那一次的错误）和 `failures_count` |
| GET | `copy-tasks/<id>/failures/` | 没复制成功的文件和文件夹，每页 50 个：`{"id", "path"（在源文件夹里的路径）, "is_dir", "size", "message", "attempts"}` |
| POST | `copy-tasks/<id>/stop/`、`copy-tasks/<id>/resume/` | 停止：已经复制的文件留着，正在运行时才行，否则 409；继续：失败或停止的任务再运行一次，目标里文件名和大小都相同的文件跳过，回答 202；别的状态 409，存储源删了或停用了也是 409 |
| GET | `copy-tasks/storages/`、`copy-tasks/folders/?storage=&path=` | 能选的存储源（启用着的）`[{"id", "name", "key", "type_label", "writable"}]`，`writable` 为 false 的不能当目标；一个文件夹里的文件夹 `{"storage", "path", "folders": [名字]}`，选文件夹用，文件夹不存在时 404 |

不用登录（没登录时按匿名用户算）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/storages/` | 自己能用的存储源，按顺序：`key`、`name`、`type`、`type_label`、`gallery_default`、`capabilities`（能不能拖动进度、移动、复制、写入（`write`，只读的存储源是 false）……）、`permissions`；没有参数，也没有根目录 |
| POST | `/api/v1/storages/<别名>/unlock/` | 输入目录密码 `{"path"（或 "rule"）, "password", "remember"}`，成功回答 200；密码不对 403 `password_wrong`，太频繁 429 `password_throttled`；不可访问的文件夹（过滤规则）403 `inaccessible`，不试密码（有「忽略隐藏」的除外）；按 `rule` 解锁只认这个人够得着的规则，别的 404 `rule_not_found` |
| DELETE | `/api/v1/storages/unlocks/` | 忘掉这个浏览器输过的目录密码（「清理密码文件夹缓存」） |

上传（不用登录：没登录时按匿名用户的权限；会话属于建它的用户，匿名用户的还属于建它的浏览器，别人的会话一律 404。`apps/uploads/views.py` 开头有完整说明）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/uploads/` | 建上传会话 `{"storage", "folder", "name", "size", "relative_path", "overwrite", "password"}`：`folder` 是用户看到的文件夹，`relative_path` 是上传文件夹时文件在里面的子文件夹（比如 `相册/2024`）。回答 201 会话：经过本站的 `{"kind": "proxy", "url", "chunk_size", "received": 0, …}`；直传的 `{"kind": "direct", "target": {"kind": "single" \| "form" \| "chunks", "url", "method", "headers", "fields", "file_field", "chunk_size", "expires_in"}, "complete_url", …}`（`target` 只在这一次回答里）；空文件当场写好，`"state": "done"`。已经有同名文件而 `overwrite` 是 false 时回答 409 `exists`，带 `can_overwrite`（有没有「删除」权限）；覆盖要「上传」和「删除」。每个 IP 每分钟最多 1200 次，每人（匿名用户每个浏览器）同时最多 20 个会话，匿名用户每个 IP 一共最多 60 个 |
| GET | `/api/v1/uploads/<id>/` | 会话：`state`（`receiving`、`waiting`、`finishing`、`failed`、`done`）、`received`、`error {"code", "detail"}`、写好的文件 `item {"name", "path", "size", "modified"}` |
| PUT | `/api/v1/uploads/<id>/` | 一块：请求体就是这块的字节，`Content-Range: bytes 起-止/总大小`，按顺序，每块最多 `chunk_size`。回答 202（还没传完）或 201（最后一块，文件已经写进存储，带 `item`）；位置不对 409 `offset`，带 `expected`（应该从哪里接着传） |
| DELETE | `/api/v1/uploads/<id>/` | 取消，删掉暂存的块，回答 204 |
| POST | `/api/v1/uploads/<id>/complete/` | 直传完了：网站查看存储里的文件，大小对上就完成；经过本站的、最后写进存储失败了的：再写一次，`{"overwrite": true}` 可以覆盖期间出现的同名文件（要「删除」权限） |
| POST | `/api/v1/uploads/folders/` | 上传文件夹时先建文件夹 `{"storage", "folder", "folders": ["相册", "相册/空的", …]}`（`folder` 要已经存在），先建上层；回答 `{"created", "existing", "failed": [{"path", "code", "detail"}]}`。要「新建文件夹」权限；每个文件夹（已经有的也算）先过过滤规则和目录密码，全部通过以后才去存储里看、去建，所以不会透露进不去的地方有什么。一次最多 1000 个、256 层 |
| PUT | `/api/v1/uploads/text/` | 在线编辑的保存 `{"storage", "path", "content", "encoding", "newline", "expected": {"modified", "size"}, "password"}`：要「上传」和「删除」（新文件只要「上传」），最大 5 MB（页面上的编辑器只编辑 2 MB 以内的文件），请求体最多 16 MB。请求体只能是这样一个对象：只有这几个字段、每个最多一次，`expected` 是对象（只有 `modified`、`size`）或 null，别的值是字符串、数字、true、false 或 null；不认识的字段、重复的字段、数组、更深的对象，读到就回答 400，后面的不再读。`expected` 和文件现在的修改时间（精确到秒）、大小对不上时回答 409 `changed`，不覆盖。`encoding`：`utf-8`、`utf-8-bom`、`gbk`、`gb18030`、`big5`、`shift_jis`、`euc-kr`、`utf-16le`、`utf-16be`（都写 BOM）、`windows-1250` 到 `windows-1258`；写出的字节在浏览器里读回来一定是发来的文字，做不到的字符回答 400 `unencodable`，说明第几行第几个字。文字里有 NUL（U+0000）时回答 400 `null_character`，也说明在第几行第几个字。`newline` 是 `lf`、`crlf`、`cr`（也可以直接写 `"\r\n"`），文件里所有的换行都改成它。回答 `{"name", "path", "size", "modified", "encoding"}`。每人（没登录的按 IP）每分钟最多 60 次；每个进程同时最多保存 2 个（读请求体、解析、写进存储），再来的等着，5 秒还没轮到回答 503，过一会儿再存 |

上传的错误码：`exists`、`folder_exists`、`parent_is_file`、`offset`、`busy`、`not_receiving`、`not_proxy`（直传的会话不收块）、`missing`（直传的文件还不在存储里）、`size_mismatch`、`changed`、`conflict`（409）；`invalid_name`、`invalid_path`、`bad_range`、`bad_length`、`short_body`、`bad_expected`、`encoding`、`unencodable`、`newline`、`too_many_folders`、`no_session`（浏览器没带会话 Cookie）、`unsupported`（400）；`not_found`（404）；`chunk_too_large`、`too_large`（413）；`too_many`（429）；`failed`（400，存储返回了别的错误，比如本地磁盘没有写的权限）；`storage_error`、`storage_auth`（502，存储出错）。存储出错时，存储说的原话（服务器的地址、FTP 服务器的回答、服务器上的路径）只给管理员看，别人看到的和文件接口一样是一句通用的话，原话写进日志；暂存文件夹的路径和系统的错误只写进日志；`no_space`（507，暂存的磁盘满了）；`spool`（暂存文件出错：500，写不进去 507，大小对不上 409）。会话的 `error.code` 还可能是 `interrupted`（写进存储时中断了）。

拒绝访问时的回答都带一个固定的 `code`，页面按它决定怎么办：`storage_not_found`（404）、`storage_disabled`、`login_required`（401）、`actor_disabled`、`no_grant`、`permission_denied`（带 `permission`）、`invalid_path`（400）、`outside_root`、`inaccessible`、`download_disabled`、`password_required` 和 `password_wrong`（带 `rule`，能按路径解锁时还带 `path`）、`password_throttled`（429，带 `wait`）、`rule_not_found`（404）。

浏览和文件操作（不用登录，没登录时按匿名用户算；都在 `/api/v1/files/<存储源别名>/` 下，`apps/files/views.py` 开头有完整说明）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `list/?path=&sort=name\|size\|time&order=asc\|desc` | 一个文件夹，一次回答：`{"storage", "path", "root", "items": [{"name", "path", "type": "file"\|"folder", "size", "modified"（带时区的 ISO 8601）, "kind", "url", "locked"}], "readme": {"html", "display", "hash"} 或 null, "permissions", "capabilities", "gallery_default", "sort"}`。`path` 都相对于自己在这个存储源里的根目录；`url` 是下载地址，没有预览和下载权限时是 null；`kind` 是文件的类别（image、video、audio、text、markdown、pdf、office、kkfileview、archive、executable、document、three_d、other）；`permissions` 是 15 种权限在这里实际用不用得上；不写排序就用显示设置里的默认排序 |
| GET | `item/?path=` | 一个文件或文件夹，和列表里的一项一样，再加 `inline_url`：本站的签名链接、浏览器里直接显示（预览用 JS 读它，存储不用设跨域）。隐藏的文件照样能这样拿到，「不可下载」的回答 403 |
| POST | `mkdir/` | 新建文件夹 `{"path", "name"}`（一层），回答 201 `{"name", "path", "type"}` |
| POST | `rename/` | 重命名 `{"path", "name", "new_name"}` |
| POST | `move/`、`copy/` | 移动、复制 `{"path", "names": [...], "target"}`（同一个存储源里），回答 `{"results": [{"name", "type", "ok", "code", "message"}], "succeeded", "failed"}`，每一项一个结果；一次最多 1000 项 |
| POST | `delete/` | 删除 `{"path", "names": [...]}`，回答同上；一次最多 1000 项 |

写操作都要 `X-CSRFToken`（没登录也要），也都要写 `path`（移动、复制还要 `target`）：根目录写 `"/"` 或 `""`，漏写、拼错回答 400，不会当成根目录（`list/` 和 `item/` 不写就是根目录）。目录密码：页面先用上面的 `/api/v1/storages/<别名>/unlock/` 输入，之后的请求就都能用；别的程序也可以每次带上密码（操作里的 `"password"`、移动和复制的目标 `"target_password"`，GET 用请求头 `X-Folder-Password`，按 UTF-8 读），只对这一个请求有效。缺密码时整个请求在动手之前就回答 403 `password_required`（带 `rule` 和能输入它的 `path`）。另外的 `code`：`not_found`（404）、`not_a_folder`、`exists`（409「目标已存在」）、`invalid_name`、`unsupported`、`into_itself`、`same_folder`（移到原来的文件夹，算成功）、`invalid`（400，没选要操作的项）、`failed`（400，存储返回了别的错误）、`storage_error` 和 `storage_auth`（502，存储那边出错，详细原因只给管理员看）。

预览（不用登录：查看器拿文件的 `inline_url`，本站签名的 `/pd/` 地址，路径带查询参数，作为能读这个文件的证明）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/preview/markdown/` | `{"url": 签名地址}`：服务器读这个 Markdown 文件（最多「文本预览的最大文件」和 2 MB 中小的那个），渲染成安全的 HTML，回答 `{"html", "size"}`。太大 400 `too_large`（带 `size`、`limit`），不是 .md 或 .markdown 400 `not_markdown` |
| POST | `/api/v1/preview/kkfileview/` | `{"url": 签名地址}`：这个文件的 kkFileView 地址 `{"url", "mode"}`，`mode` 是 `iframe`（内嵌）或 `new_tab`；没设 kkFileView 服务地址 404 `kkfileview_off`，kkFileView 和网站同一个来源 400 `kkfileview_same_origin` |

不是本站的 `/pd/` 地址、签名不对（存储源已经删了也算）是 403 `link_invalid`，过期是 403 `link_expired`；存储源停用了、过滤规则不让读时是访问策略的 `code`（`storage_disabled`、`inaccessible`、`download_disabled`）；文件不在了 404 `not_found`，存储出错 502 `storage_error`。两个接口每个用户（没登录按地址）每分钟 60 次。

直链和短链（`apps/links/views.py`、`apps/links/manage_views.py` 开头有完整说明）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/links/<存储源别名>/path/` | 不用登录（没登录时按匿名用户算）：直链 `{"paths": [文件的路径，相对于自己的根目录], "password", "qr"}`，回答 `{"links": [{"path", "name", "url"}], "qr"}`。每个文件都先按打开时的规则检查（获取直链、下载、根目录、过滤规则、目录密码），有一个不行就整个拒绝，回答和文件接口一样的 `code`；设置里关了直链时 403 `path_links_off`（关了短链时短链接口是 `short_links_off`），路径是根目录时 400 `not_a_file`；一次最多 1000 个；不检查文件在不在。`url` 是完整网址（`[site] url`，没写时用请求的地址）。`qr: true` 并且只有一个文件时带二维码 `{"svg", "png"}`（data: 地址），链接太长时是 null。这两个接口每人每分钟一共最多 60 次（没登录的按 IP） |
| POST | `/api/v1/links/<存储源别名>/short/` | 不用登录：短链 `{"paths": [...], "expires": 秒数, "password", "qr"}`，`expires` 必须是设置里一个有效期的秒数（永久是 -1），不是的回答 400 `invalid_expiry`；回答 201 `{"links": [{"path", "name", "key", "url", "expires_at"}], "qr"}`。每个文件都检查「生成短链」「下载」权限、根目录、过滤规则和目录密码，全部通过才一起建，否则一个也不建。每人每分钟最多生成 1000 个（没登录的访客一起算），超过回答 429 `too_many_links` |
| GET | `/api/v1/manage/short-links/` | 管理员：短链，`?storage=`（存储源 ID）、`?path=`、`?key=`（包含，不分大小写）、`?since=`、`?until=`（创建日期，含当天）、`?ordering=`（`created_at`、`expires_at`、`key`、`id`，前面加 `-` 倒序；按过期时间排时永久的在最后）、`?page=`；每条 `{"id", "key", "url", "storage", "path", "created_by", "created_at", "expires_at", "expired"}` |
| DELETE | `/api/v1/manage/short-links/<id>/` | 管理员：删除一个（它的下载日志留着），回答 `{"deleted": 1}` |
| POST | `/api/v1/manage/short-links/delete/` | 管理员：`{"ids": [...]}` 删除选中的（一次最多 1000 个），回答 `{"deleted": 个数}` |
| POST | `/api/v1/manage/short-links/delete-expired/` | 管理员：删除现在已经过期的短链和它们的下载日志（同一个事务），回答 `{"deleted": 短链数, "logs": 日志条数}` |
| GET | `/api/v1/manage/short-links/export/` | 管理员：和列表一样的筛选，回答全部匹配的短链（不分页），CSV，UTF-8 带 BOM，`=`、`+`、`-`、`@` 开头的格子前面加 `'`，防止表格软件当成公式 |
| GET | `/api/v1/manage/link-limits/` | 管理员：下载频率限制的「统计信息」：`{"seconds", "downloads", "count", "windows": [{"address", "count", "started_at", "expires_at"}]}`，次数最多的在前，最多 500 个 |
| GET | `/api/v1/manage/download-logs/` | 管理员：下载日志，新的在前，`?storage=`、`?kind=path/short/page`、`?key=`、`?path=`、`?user=`、`?user_agent=`、`?referer=`（包含，不分大小写）、`?ip=`（开头）、`?since=`、`?until=`；回答里的 `newest` 是符合条件的最新一条的 ID；每条带 `username`（下载时登录着的账号，没有是 `""`）和 `url`（短链，或者用现在的前缀算出的直链；页面下载是 `null`），`ip_location` 是 IP 的归属地，规则同用户列表的 `last_login_location` |
| DELETE | `/api/v1/manage/download-logs/` | 管理员：删除同样的条件查到的全部，带上列表给的 `?newest=` 时只删到那一条为止（列表显示以后才记下的不删），回答 `{"deleted": 条数}`。不认识的参数和请求体都回答 400，什么也不删 |
| POST | `/api/v1/manage/download-logs/delete/` | 管理员：`{"ids": [...]}` 删除选中的（一次最多 1000 条），回答 `{"deleted": 条数}` |

分享（`apps/shares/views.py` 开头有完整说明）。自己的分享要登录（匿名用户不行）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/shares/` | 我的分享：`?keyword=`（Key 或名称，不分大小写）`&storage=`（存储源 id）`&status=all\|active\|expired&since=&until=`（创建的日期）`&page=`，新的在前。每一条 `{"key", "url"（完整的分享链接）, "label", "entries": [{"name", "type"}], "entry_count", "storage", "folder"（存储源里的路径）, "has_password", "password", "password_unreadable", "created_at", "expires_at", "expired", "access_count", "download_count", "can_delete"}`：密码只给分享者自己和管理员 |
| POST | `/api/v1/shares/` | 创建 `{"storage", "path"（当前文件夹，和文件接口一样从自己的根目录算）, "entries": [{"name", "type"}], "key", "password", "expiry": "forever"\|"7d"\|"30d"\|"custom", "expires_at"（自定义时；不写 expiry 时有它就是自定义，expiry 是别的时回答 400）}`，回答 201 和上面一样的一条。要「创建分享链接」（每一项和文件夹都按访问策略检查，包括过滤规则和目录密码），自己定 Key 还要「自定义分享链接 key」；一次最多 1000 项，Key 被占用 409 `key_taken`，被过滤规则隐藏的项 400 `hidden` |
| POST | `/api/v1/shares/clear-expired/` | 删除自己过期的分享 `{"deleted"}` |
| DELETE | `/api/v1/shares/<key>/` | 删除一个分享（分享者本人或超级管理员），回答 204 |

打开分享的人（不用登录）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/shares/<key>/` | 分享的信息。输入密码以前只有 `{"key", "need_password", "verified", "expired", "expires_at", "entry_count", "owner"（分享者的昵称）}`；之后多 `"entries"`、`"storage"`、`"created_at"`。不存在 404 `share_not_found`，过期 404 `share_expired` |
| POST | `/api/v1/shares/<key>/verify/` | `{"password"}`：对了回答 `{"verified": true}`，设上这个分享的 Cookie；错了 403 `share_password_wrong`；试错太多 429 `share_password_throttled`（`wait` 秒） |
| GET | `/api/v1/shares/<key>/list/?path=&sort=&order=` | 分享里的一个文件夹，和文件接口的列表一样（`root`、`readme` 是 null）：根目录只有分享的那几项；每个文件的 `url` 是 `/share/<key>/preview?path=…`（分享者有预览权限时，否则是下载的），`download_url` 是 `/share/<key>/download?path=…`（有下载权限时）。没输密码 403 `share_password_required` |
| GET | `/api/v1/shares/<key>/item/?path=` | 一个文件或文件夹；分享者有预览权限的文件带 `inline_url`（本站的签名链接，给预览用） |
| POST | `/api/v1/shares/<key>/unlock/` | 分享里的目录密码 `{"path" \| "rule", "password"}`，只对这个分享有效 |
| DELETE | `/api/v1/shares/<key>/unlocks/` | 忘掉在这个分享里输过的目录密码 |

另外的 `code`：`share_unavailable`（403，分享者现在不能分享了）、`share_password_empty`（400）、`share_password_unreadable`（409，`secret_key` 换过，要重新创建分享）；删除别人的分享是 403 `not_owner`。

管理员：`GET /api/v1/manage/shares/`（所有人的分享，同样的筛选加 `&user=` 用户名或昵称，每一条多 `owner`），`DELETE /api/v1/manage/shares/<key>/`（分享者本人或超级管理员），`POST /api/v1/manage/shares/clear-expired/`（所有人过期的分享）。

分享的页面：`/share/<key>`（`?path=` 是分享里的文件夹）；`/share/<key>/download?path=` 下载一个文件、`/share/<key>/preview?path=` 在浏览器里显示它，都是检查以后跳转（302，不缓存）到文件的下载地址，每次算一次下载；没输密码时跳到分享页。`/shares/` 是「我的分享」。

OnlyOffice（`apps/office/views.py` 开头有完整说明）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/office/config/` | 在 OnlyOffice 里打开一个文件的配置：`{"storage", "path", "password"}`（`password` 是可选的目录密码），分享里是 `{"share", "path"}`（只读）；可选的 `mobile`、`dark`（true 时用 OnlyOffice 的手机版界面、深色主题，和别的一起签名）。不用登录（按匿名用户的权限），要 `X-CSRFToken`；要「下载」权限，过滤规则和目录密码照常。回答就是 OnlyOffice 的配置：`document`（`fileType`、`key`、`title`、`url`、`permissions`）、`documentType`、手机上的 `type`、`editorConfig`（`callbackUrl`、`lang`（页面的语言：`zh-CN` 或 `en`）、`mode`、`user`；能编辑时（`forcesave`）或要深色主题时（`uiTheme`）还有 `customization`），设了 Secret 时还有 `token`（用它以 HS256 签过整个配置的 JWT）。错误码：`onlyoffice_off`（404，没设 OnlyOffice 地址）、`not_office`、`not_a_file`、`not_found`（404）、`busy`（409）、`closing`（409，刚编辑完，OnlyOffice 还在保存）、`storage_error`（502），以及访问策略和分享的那些。每人（没登录的每个 IP）每分钟最多 60 次 |
| GET | `/onlyoffice/edit/?storage=<别名>&path=<路径>` | 编辑器的页面（分享里是 `?share=<分享 key>&path=<路径>`），文件页在新标签页里打开它：它要来上面的配置，加载 OnlyOffice 的 `api.js`，打开编辑器 |
| POST | `/onlyoffice/callback/` | OnlyOffice 服务器的回调（保存）：不用登录、不查 CSRF，靠 JWT；只回答 `{"error": 0}` 或 `{"error": 1}` |

文件浏览和下载的地址：`/<存储源别名>/<路径>` 是文件夹的页面（排在所有别的地址后面，别名不会和网站自己的地址重名，见设计说明）；`/pd/<存储源别名>/<路径>?e=…&sign=…` 是本站的签名下载链接，不用登录，签名就是许可（支持 GET、HEAD 和 Range）。页面上的「下载」按钮打开它时加上 `dl=1`（不在签名里）：从头开始的 GET（没有 Range，或者从第 0 个字节开始）成功时记一条下载日志（页面下载；和直链一样，同一个 IP、同一个用户 10 分钟内同一个文件只记一条），这样的回答带 `Cache-Control: private, no-store`，每点一次都会到网站。存储源同时发送的下载满了时，按它的 `proxy_when_full` 跳到存储源自己的下载地址（302），或者回答 429；一个 IP 同时的下载满了时回答 429；429 都带 `Retry-After: 5`（HEAD 不算，不受这两个限制）。存储出错时是 500 的错误页「从存储读取文件失败」；管理员登录着打开时，页面上还写着存储说的原因（和文件接口一样），控制台里每次记一行 `Download failed (storage <别名>): <原因>`。

直链和短链的地址：`/<直链前缀>/<存储源别名>/<存储源里的完整路径>`（前缀不分大小写）和 `/s/<Key>`（Key 分大小写），只收 GET 和 HEAD。能下载时回答 302 跳到文件的下载地址（`.m3u8` 回答播放列表本身，超过 2 MB 的回答 403），不能时是网站的错误页：429（频率限制，或者猜错的短链 Key 太多，带 `Retry-After`）、403（防盗链、开关关着、没有权限、短链已失效）、401（直链要登录）、404（存储源、文件或短链不存在）、502（存储源给不出下载地址）。同一个 IP、同一个用户 10 分钟内打开同一个链接只记一条下载日志，HEAD 和带 Range 的也算打开。带 `If-Modified-Since`（没有 `If-None-Match`）、文件从那以后没改过（按存储源给的修改时间，也就是 `/pd/` 的 `Last-Modified`）时回答 304，不跳转，也不记下载日志。每个回答都带 `Cache-Control: no-cache, no-store, must-revalidate, private`。

不在 `/api/` 下的地址：`/site/custom.css` 和 `/site/custom.js` 是显示设置里的自定义 CSS 和 JS（公开页面用 `?v=<指纹>` 引用，内容变了地址就变，浏览器缓存一年；没设时 404）。`/sso/<简称>/login?next=/路径` 去服务商那里登录（303），`/sso/<简称>/login/callback` 是服务商回来的地址（在服务商那里登记的就是它）；这两个每分钟最多 30 次（没登录的按 IP，登录了的按用户），出错时是网站的错误页。

状态码约定：400 参数不对，或者请求头 Content-Type 读不懂（太长，或者 RFC 2231 写法的参数 `x*=编码''值` 用了 Python 不认识的编码）；401 没登录；403 没有权限、CSRF 不对，或者被访问控制挡住（`"code": "blocked"`）；404 没有找到；405 不支持这个方法；409 现在的状态不允许（比如已经绑定了、同名文件已经存在）；413 请求太大（一个请求最多 64 MB，其中 JSON 和表单最多 2.5 MB，在线编辑、创建分享等另有上限）；415 不是 JSON 也不是 multipart（上传的块除外），或者字符集不是 UTF-8；429 太频繁了（频率限制和试错太多的回答带 `Retry-After`，同时上传太多的 `too_many`、生成短链太多的 `too_many_links` 不带）；502 网站去问的别的服务器出错了（存储源、单点登录的服务商）；503 网站还没有安装（安装模式下所有接口都是 503），或者同时在保存的在线编辑太多了（过一会儿再存）；507 上传暂存的磁盘满了。
