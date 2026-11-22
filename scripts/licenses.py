"""The license texts of what PyInstaller put into the program, for scripts/build.sh (BUILD-05):

    python scripts/licenses.py <PyInstaller's work folder of the program> <HFile's source folder> <file to write>

HFile's own LICENSE and NOTICE come first, then Python's, then those of every Python library a file of
which went into the program: the files of its distribution named LICENSE, LICENCE, COPYING or NOTICE,
also those of what it carries along (the jQuery in Django's admin). What went in is read from the .toc
files PyInstaller leaves in its work folder, which list every module, extension and data file with the
path it was taken from. The native libraries of the system or of conda (OpenSSL, SQLite, the MySQL client
library, Pillow's image libraries) belong to no Python distribution: they are named at the end.
"""

import ast
import importlib.metadata
import sys
import sysconfig
from pathlib import Path

LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
LINE = "=" * 78


def taken(work):
    """{path: kind} of what PyInstaller took: its .toc files hold (name, path, kind) tuples, nested in lists."""
    found = {}

    def walk(value):
        if isinstance(value, (tuple, list)):
            if len(value) == 3 and all(isinstance(item, str) for item in value) and Path(value[1]).is_absolute():
                found[str(Path(value[1]).resolve())] = value[2]
            else:
                for item in value:
                    walk(item)

    for toc in sorted(Path(work).glob("*.toc")):
        walk(ast.literal_eval(toc.read_text(encoding="utf-8")))
    return found


def owners():
    """{path: distribution} for every file of every installed distribution."""
    found = {}
    for dist in importlib.metadata.distributions():
        for file in dist.files or ():
            found.setdefault(str(Path(dist.locate_file(file)).resolve()), dist)
    return found


def section(title, texts):
    """A title, then each (name, text)."""
    lines = [LINE, title, LINE]
    for name, text in texts:
        lines += ["", f"--- {name} ---", "", text.rstrip()]
    return "\n".join(lines) + "\n\n"


def main(work, source, out):
    files = taken(work)
    by_path = owners()
    dists = {}
    for path in files:
        dist = by_path.get(path)
        if dist is not None:
            dists.setdefault(dist.metadata["Name"].lower(), dist)
    # Python's library (lib/python3.12; Lib on Windows, with its extensions in DLLs), and its license there or,
    # on Windows, next to python.exe: LICENSE_PYTHON.txt with conda-forge's Python, LICENSE.txt with
    # python-build-standalone's (Windows on Arm, .github/actions/windows-arm64-python).
    python = Path(sysconfig.get_paths()["stdlib"]).resolve()
    own = (python, Path(sys.base_prefix, "DLLs").resolve())
    python_license = next(path for path in (python / "LICENSE.txt", Path(sys.base_prefix, "LICENSE_PYTHON.txt"),
                                            Path(sys.base_prefix, "LICENSE.txt")) if path.is_file())
    parts = [
        "HFile 单文件程序里的软件和它们的许可证（scripts/build.sh 打包时生成，和程序放在一起发布）。\n\n",
        section("HFile", [(name, Path(source, name).read_text(encoding="utf-8")) for name in ("LICENSE", "NOTICE")]),
        section(f"Python {sys.version.split()[0]}", [(python_license.name, python_license.read_text(encoding="utf-8"))]),
    ]
    without = []
    for key in sorted(dists):
        dist = dists[key]
        texts = [
            (str(file), Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace"))
            for file in dist.files or () if file.name.upper().startswith(LICENSE_NAMES)
        ]
        if not texts:
            without.append(dist.metadata["Name"])
            said = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "（没有写）"
            texts = [("元数据里的许可证", f"这个发行包里没有许可证文件，它的元数据写的是：{said}")]
        parts.append(section(f"{dist.metadata['Name']} {dist.version}", texts))
    native = sorted({
        Path(path).name for path, kind in files.items()
        if kind in ("BINARY", "EXTENSION") and path not in by_path
        and not any(Path(path).is_relative_to(folder) for folder in own)
        and not Path(path).name.startswith(("libpython", "python3"))  # Python's, above (python312.dll on Windows)
    })
    if native:
        parts.append(section("本地库", [("打包环境里的（系统的或者 conda 的），许可证见各自的项目", "\n".join(native))]))
    Path(out).write_text("".join(parts), encoding="utf-8")
    print(f"许可证：{len(dists)} 个 Python 库，另有 {len(native)} 个本地库，写在 {out}。")
    if without:
        print(f"注意：这些库的发行包里没有许可证文件，只写了元数据里的许可证：{'、'.join(without)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
