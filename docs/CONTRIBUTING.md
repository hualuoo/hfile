# 参与开发

简体中文 | [English](CONTRIBUTING.en.md)

欢迎报告问题、提建议、改文档和提交代码。

## 报告问题

先在 [Issues](https://github.com/hualuoo/hfile/issues) 里搜一下有没有人报告过。

新建 Issue 时选「问题报告」，写清楚：

- HFile 的版本（`hfile --version`，或者管理页「系统」下的「版本与升级」）、怎么运行的（单文件程序还是从源码）、系统、数据库（SQLite 还是 MySQL，哪个版本）；
- 和哪种存储源有关（本地、S3、OneDrive……）；
- 怎么一步步让它出现，本来应该怎样，实际是怎样；
- 程序的输出或日志里相关的几行。

贴出来之前，去掉密钥、令牌、密码、`config.toml` 的内容，和不想公开的域名、路径。

## 提建议

新建 Issue 时选「功能建议」。先说想做成什么事、现在为什么做不到，再说你想要的做法。

## 搭开发环境

需要 conda（比如 [Miniforge](https://github.com/conda-forge/miniforge)）；用 MySQL 时还要 Docker。

```bash
git clone https://github.com/hualuoo/hfile.git
cd hfile
conda env create -f environment.yml
conda activate hfile
pip install -r requirements.lock        # 间接依赖也换成测试过的版本
python -B manage.py check               # 新建 config.toml，默认用 SQLite
```

在 `config.toml` 的 `[site]` 里填上 `secret_key`（至少 32 个字符，比如 `python -c "import secrets; print(secrets.token_urlsafe(50))"` 的输出），并设 `debug = true`。然后：

```bash
python -B manage.py upgrade             # 建表
python -B manage.py createsuperuser     # 第一个管理员
python -B manage.py runserver           # 打开 http://127.0.0.1:8000/
```

调试模式下，`/components/` 把浅色、深色两种主题的全部组件并排列出来，写页面时照着用。本地的 MySQL 容器、在开发机上试安装向导这些，见[开发](development.md)。

## 跑测试

测试用设置文件里的数据库，所以要有一个填了 `secret_key` 的 `config.toml`：

```bash
HFILE_CONFIG=/path/to/config.toml python -B manage.py test --parallel
```

- SQLite 上加 `--parallel` 并行跑，快得多。
- 改了和数据库有关的代码，也要在 MySQL 8.4 上跑一遍：`scripts/dev-db.sh up` 起一个本地容器，`scripts/dev-db.sh use 设置文件 hfile_自己的名字` 让设置文件指向它。没有 `mysqldump` 和 `mysql` 命令时去掉 `--parallel`，串行跑。
- 浏览器测试（带 `browser` 标签）要有 Google Chrome，没有时跳过：只跑它们用 `--tag browser`，不跑用 `--exclude-tag browser`。
- 每个 Pull Request 都由 CI 在 SQLite 和 MySQL 8.4 上各跑一遍全部测试，浏览器测试另外跑一遍（见[开发](development.md#测试)）。

## 提交代码

1. Fork 仓库，从 `main` 拉一个新分支。
2. 一个 Pull Request 只做一件事；改动大的，先开 Issue 商量做法。
3. 修问题时，先写一个能复现它的测试，再改代码让它通过。
4. 提交之前确认：
   - 测试在 SQLite 上全部通过；改了和数据库有关的代码，MySQL 上也通过；
   - 改了行为，就同时改 `docs/` 里的文档；
   - 新的数据库迁移要能在已有的数据上跑通，已经发布的迁移不再修改；
   - 换了依赖的版本：装好、两种数据库上跑完测试，再运行 `python scripts/lock.py` 重新生成 `requirements.lock`，一起提交。
5. 提交说明用中文，标题写成「类型（范围）：做了什么」，比如 `修正（上传）：…`、`文档（README）：…`。

## 代码约定

- 页面上的文字、错误信息和 `config.toml` 里的说明在代码里用英文写，放在 `gettext()`（模板里是 `{% translate %}`）里，中文翻译在 `config/locale/zh_Hans/`，见[翻译](development.md#翻译)。代码里的注释和文档字符串用英文，`docs/` 里的文档用中文。
- 新的存储源从复制示例驱动 `apps/storages/drivers/example.py` 开始，要加的文件、要登记的地方和驱动的写法见[添加存储源](development.md#添加存储源)。
- 前端不用构建，也不走 CDN：页面是 Django 模板加原生 JavaScript。引入的前端库原样放在 `apps/preview/static/preview/vendor/` 下，带上它的许可证文件，并写进 [NOTICE](../NOTICE)。
- 只改要改的：不顺手改无关的代码和格式，不加用不到的设置项。

## 许可证

提交到本仓库的代码和文档，都按 [Apache License 2.0](../LICENSE) 发布。
