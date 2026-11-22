# Contributing

[简体中文](CONTRIBUTING.md) | English

Bug reports, suggestions, documentation fixes and code are all welcome. HFile's detailed documentation (`docs/`) is in Chinese; issues and pull requests in English are fine.

## Reporting a bug

First search the [issues](https://github.com/hualuoo/hfile/issues) to see whether it has been reported.

Open a new issue with the bug report template and include:

- the HFile version (`hfile --version`, or 版本与升级 under 系统 on the admin page), how you run it (single-file program or from source), the OS, and the database (SQLite or MySQL, and which version);
- the storage type involved (local, S3, OneDrive, …);
- the steps that make it happen, what you expected, and what happened instead;
- the relevant lines of the program's output or log.

Before posting, remove keys, tokens, passwords, the contents of `config.toml`, and any domain names or paths you don't want to make public.

## Suggesting a feature

Open a new issue with the feature request template. Describe what you are trying to do and why you can't do it now, then the solution you'd like.

## Setting up a development environment

You need conda (for example [Miniforge](https://github.com/conda-forge/miniforge)), and Docker if you want MySQL.

```bash
git clone https://github.com/hualuoo/hfile.git
cd hfile
conda env create -f environment.yml
conda activate hfile
pip install -r requirements.lock        # indirect dependencies at the tested versions too
python -B manage.py check               # creates config.toml (SQLite by default)
```

In the `[site]` section of `config.toml`, fill in `secret_key` (at least 32 characters, for example the output of `python -c "import secrets; print(secrets.token_urlsafe(50))"`) and set `debug = true`. Then:

```bash
python -B manage.py upgrade             # creates the tables
python -B manage.py createsuperuser     # the first administrator
python -B manage.py runserver           # open http://127.0.0.1:8000/
```

In debug mode, `/components/` shows every UI component in the light and dark themes side by side; use it as the reference when building pages. The local MySQL container, trying the setup wizard on a development machine and more are covered in [development](development.md) (Chinese).

## Running the tests

The tests use the database named in the config file, so you need a `config.toml` with `secret_key` filled in:

```bash
HFILE_CONFIG=/path/to/config.toml python -B manage.py test --parallel
```

- On SQLite, `--parallel` makes the run much faster.
- If you changed anything database-related, run the tests on MySQL 8.4 as well: `scripts/dev-db.sh up` starts a local container, and `scripts/dev-db.sh use <config file> hfile_<your name>` points the config file at it. Without the `mysqldump` and `mysql` commands, drop `--parallel` and run them serially.
- The browser tests (tagged `browser`) need Google Chrome and are skipped without it: `--tag browser` runs only them, `--exclude-tag browser` leaves them out.
- Every pull request is tested by CI: on SQLite on Linux, Windows and macOS, each on x86-64 and on arm64, and on MySQL 8.4 on Linux; the browser tests run in a job of their own (see [Development](development.md#测试)).

## Submitting changes

1. Fork the repository and create a branch from `main`.
2. Keep each pull request to one change; for a large change, open an issue first to agree on the approach.
3. When fixing a bug, first write a test that reproduces it, then change the code until it passes.
4. Before submitting, make sure that:
   - all tests pass on SQLite, and on MySQL too if you changed anything database-related;
   - behaviour changes come with matching changes to the documentation in `docs/`;
   - new database migrations run on existing data, and released migrations are never modified;
   - if you changed a dependency version, you installed it, ran the tests on both databases, then regenerated `requirements.lock` with `python scripts/lock.py` and committed it with the change.
5. Commit messages are written in Chinese, with a title of the form 类型（范围）：做了什么 ("type (scope): what changed"), for example `修正（上传）：…` or `文档（README）：…`.

## Conventions

- Text on pages, error messages and the comments in `config.toml` are written in English inside `gettext()` (`{% translate %}` in templates), and translated into Chinese in `config/locale/zh_Hans/`; see [translation](development.md#翻译) (Chinese). Comments and docstrings in the code are in English; the documentation in `docs/` is in Chinese.
- A new storage type starts as a copy of the example driver, `apps/storages/drivers/example.py`; the files to add, the places to register the type and how drivers are written are in [adding a storage type](development.md#添加存储源) (Chinese).
- No front-end build step and no CDN: pages are Django templates with plain JavaScript. Third-party front-end libraries go, unmodified, under `apps/preview/static/preview/vendor/`, with their license files, and are listed in [NOTICE](../NOTICE).
- Change only what the task needs: no drive-by changes to unrelated code or formatting, and no settings nobody needs.

## License

Code and documentation contributed to this repository are released under the [Apache License 2.0](../LICENSE).
