"""requirements.lock: every Python package of the environment HFile is tested and built in, direct and indirect,
at the version installed here (BUILD-04). requirements.txt and requirements-dev.txt name only the direct ones;
what those require came in at whatever version was the newest on the day it was installed.

    python scripts/lock.py                   writes requirements.lock from this environment
    python scripts/lock.py --check [folder]  compares this environment with the requirements.lock in `folder`
                                             (default: the project's), exit status 1 when they differ

The packages: those of requirements.txt and requirements-dev.txt, with the extras they ask for, mysqlclient and
Pillow (from conda, environment.yml), and what each of them requires, recursively, the environment markers
evaluated here. scripts/build.sh checks the commit's lock before it builds anything. To change a version:
install it, run the tests on both databases, then write the lock again and commit it with the change.

Linux, macOS and Windows each need a few packages of their own (Windows: pywin32 for docker, pefile and
pywin32-ctypes for PyInstaller; macOS: macholib for PyInstaller). Those have a marker in the lock, which pip follows
(pefile==2024.8.26 ; sys_platform == "win32"): written by lock.py on the system that needs them, and kept as they are
when it runs on the others.
"""

import importlib.metadata
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
FILES = ("requirements.txt", "requirements-dev.txt")
FROM_CONDA = ("mysqlclient", "Pillow")  # environment.yml; requirements.txt says so in a comment
HEADER = """\
# HFile 测试和打包用的环境里全部的 Python 包，直接的和间接的（scripts/lock.py 生成，不要手改）。
# 带 sys_platform 的只有那个系统要：在那个系统上运行 lock.py 时写进来，在别的系统上运行时原样留着。
# scripts/build.sh 打包前照它检查环境，不一样就不打包。照它装：pip install -r requirements.lock
"""
# The variables of the environment markers on each system the program is built for.
SYSTEMS = {
    "linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
    "darwin": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"},
    "win32": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt"},
}
HERE = sys.platform if sys.platform in ("darwin", "win32") else "linux"


def closure(folder, system=HERE):
    """({canonical name: distribution}, [names required but not installed]) of the environment `folder`'s
    requirements files describe on `system` (another one's: as far as the packages installed here tell)."""
    todo = []
    for name in FILES:
        for line in (folder / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                requirement = Requirement(line)
                todo.append((requirement.name, set(requirement.extras)))
    todo += [(name, set()) for name in FROM_CONDA]
    found, extras, missing = {}, {}, []
    while todo:
        name, wanted = todo.pop()
        key = canonicalize_name(name)
        if key in found and wanted <= extras[key]:
            continue
        try:
            dist = found.get(key) or importlib.metadata.distribution(key)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
            continue
        found[key], extras[key] = dist, extras.get(key, set()) | wanted
        for text in dist.requires or ():
            requirement = Requirement(text)
            if requirement.marker is None or any(requirement.marker.evaluate({**SYSTEMS[system], "extra": extra})
                                                 for extra in extras[key] | {""}):
                todo.append((requirement.name, set(requirement.extras)))
    return found, sorted(set(missing))


def locked(path):
    """{canonical name: (name, version, marker)} of a requirements.lock; the marker "" for every system."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pin, _, marker = line.partition(";")
            name, _, version = pin.strip().partition("==")
            pins[canonicalize_name(name)] = (name, version, marker.strip())
    return pins


def main(args):
    check = args[:1] == ["--check"]
    folder = Path(args[1]) if check and len(args) > 1 else ROOT
    found, missing = closure(folder)
    elsewhere = {key for system in SYSTEMS if system != HERE for key in closure(folder, system)[0]}
    own = f'sys_platform == "{HERE}"'  # the marker of a package only this system needs
    now = {key: (dist.metadata["Name"], dist.version, "" if key in elsewhere else own) for key, dist in found.items()}
    pins = locked(folder / "requirements.lock")
    if not check:
        if missing:
            sys.exit(f"这些包没有装，锁定不了：{'、'.join(missing)}")
        theirs = [pin for key, pin in pins.items() if pin[2] not in ("", own) and key not in now]  # the other systems'
        lines = sorted([*now.values(), *theirs], key=lambda pin: pin[0].lower())
        (ROOT / "requirements.lock").write_text(
            HEADER + "".join(f"{name}=={version}{f' ; {marker}' if marker else ''}\n" for name, version, marker in lines),
            encoding="utf-8", newline="\n")  # the same file whichever system writes it
        print(f"写好了 requirements.lock：{len(lines)} 个包，这个系统要的 {len(now)} 个。")
        return
    pins = {key: pin for key, pin in pins.items() if pin[2] in ("", own)}  # what this system installs
    problems = [f"{name}：没有装" for name in missing]
    for key in sorted(pins.keys() | now.keys()):
        if key not in now:
            if key not in {canonicalize_name(name) for name in missing}:
                problems.append(f"{pins[key][0]}：requirements.lock 里有，requirements 里已经不要它了")
        elif key not in pins:
            problems.append(f"{now[key][0]} {now[key][1]}：requirements 要它，requirements.lock 里没有")
        elif now[key][1] != pins[key][1]:
            problems.append(f"{now[key][0]}：装的是 {now[key][1]}，锁定的是 {pins[key][1]}")
        elif now[key][2] != pins[key][2]:
            problems.append(f"{now[key][0]}：{'只有这个系统要它' if now[key][2] else '每个系统都要它'}，requirements.lock 里写的不对")
    if problems:
        sys.exit(
            "环境和 requirements.lock 不一样：\n" + "".join(f"  - {problem}\n" for problem in problems)
            + "照它装：pip install -r requirements.lock。要换版本的话，装好、两种数据库上跑完测试，再运行 "
            "python scripts/lock.py 重新生成，和改动一起提交。"
        )
    print(f"环境和 requirements.lock 一样：{len(pins)} 个包。")


if __name__ == "__main__":
    # In Chinese wherever it goes: on Windows what Python prints into a pipe or a file (CI, build.sh) is in the
    # system's code page, which may have no Chinese (cp1252); UTF-8 there, as config/cli.py does.
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
