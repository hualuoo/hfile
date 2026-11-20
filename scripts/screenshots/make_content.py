"""Demo content for HFile's README screenshots (run.sh): the same tree in Chinese (content-zh) and English
(content-en), and the photos in it.

    python make_content.py OUT_DIR   ->  OUT_DIR/photos, OUT_DIR/content-zh, OUT_DIR/content-en

The photos are drawn here (skies, a sun or moon, ridges of mountains or dunes), nothing real. Everything is the
same on every run (seeded drawing, fixed modification times), and so are the screenshots.
"""

import math
import os
import random
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFilter

TZ = ZoneInfo("Asia/Shanghai")
W, H = 1600, 1000
S = 2  # drawn at twice the size, then scaled down: smooth edges


def when(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sky(stops):
    img = Image.new("RGB", (W * S, H * S))
    draw = ImageDraw.Draw(img)
    for y in range(H * S):
        t = y / (H * S - 1)
        color = stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                color = lerp(c0, c1, (t - p0) / (p1 - p0))
                break
        draw.line([(0, y), (W * S, y)], fill=color)
    return img.convert("RGBA")


def sun(img, x, y, r, color, glow_color, glow_r, glow_alpha):
    layer = Image.new("RGBA", img.size, glow_color + (0,))  # the glow's color everywhere: the blur only fades it
    draw = ImageDraw.Draw(layer)
    gx, gy, gr = x * W * S, y * H * S, glow_r * S
    draw.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=glow_color + (glow_alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(gr / 2.2))
    img = Image.alpha_composite(img, layer)
    draw = ImageDraw.Draw(img)
    rr = r * S
    draw.ellipse([gx - rr, gy - rr, gx + rr, gy + rr], fill=color + (255,))
    return img


def stars(img, rng, count, top):
    draw = ImageDraw.Draw(img)
    for _ in range(count):
        x, y = rng.uniform(0, W * S), rng.uniform(0, top * H * S)
        r = rng.choice([1, 1, 1, 2, 2, 3])
        a = rng.randint(90, 255)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 240, a))
    return img


def ridges(img, rng, near, far, layers, *, top, bottom, amp, waves):
    draw = ImageDraw.Draw(img)
    for i in range(layers):
        t = i / max(layers - 1, 1)
        color = lerp(far, near, t)
        base = (top + (bottom - top) * t) * H * S
        phases = [rng.uniform(0, 2 * math.pi) for _ in waves]
        points = []
        for x in range(0, W * S + 8, 8):
            y = base
            for (freq, weight, sharp), phase in zip(waves, phases):
                s = math.sin(2 * math.pi * freq * x / (W * S) + phase)
                y -= amp * H * S * weight * ((1 - 2 * abs(s)) if sharp else s)
            points.append((x, y))
        draw.polygon(points + [(W * S, H * S), (0, H * S)], fill=color + (255,))
    return img


MOUNTAINS = [(0.9, 0.55, False), (2.3, 0.30, False), (4.7, 0.16, True), (9.1, 0.04, False)]
DUNES = [(0.6, 0.6, False), (1.4, 0.3, False)]

# (sky stops, sun or None, stars, ridge colors near/far, layers, top, bottom, amplitude, waves)
PHOTOS = [
    ([(0, (104, 198, 244)), (0.62, (226, 243, 251)), (1, (226, 243, 251))],
     (0.56, 0.32, 26, (255, 255, 255), (170, 225, 250), 170, 210), 0, (40, 84, 104), (196, 210, 220), 4, 0.48, 0.80, 0.05, MOUNTAINS),
    ([(0, (136, 194, 158)), (0.58, (222, 236, 200)), (1, (222, 236, 200))],
     (0.50, 0.44, 30, (255, 252, 222), (240, 250, 200), 200, 200), 0, (18, 70, 46), (128, 182, 142), 5, 0.44, 0.86, 0.05, MOUNTAINS),
    ([(0, (246, 146, 36)), (0.60, (251, 198, 30)), (1, (251, 198, 30))],
     (0.40, 0.46, 32, (255, 244, 176), (255, 236, 120), 210, 190), 0, (96, 30, 12), (214, 142, 84), 5, 0.50, 0.88, 0.045, MOUNTAINS),
    ([(0, (12, 18, 48)), (0.70, (30, 44, 96)), (1, (30, 44, 96))],
     (0.76, 0.20, 18, (246, 242, 205), (210, 210, 170), 60, 140), 260, (4, 8, 20), (40, 54, 100), 4, 0.54, 0.86, 0.04, MOUNTAINS),
    ([(0, (246, 208, 98)), (0.58, (246, 168, 124)), (1, (246, 168, 124))],
     (0.24, 0.36, 36, (255, 246, 212), (255, 222, 150), 230, 200), 0, (140, 72, 30), (220, 166, 112), 4, 0.60, 0.92, 0.03, DUNES),
    ([(0, (88, 93, 104)), (0.62, (202, 202, 206)), (1, (202, 202, 206))],
     None, 0, (38, 44, 54), (172, 174, 180), 4, 0.50, 0.84, 0.05, MOUNTAINS),
    ([(0, (150, 182, 242)), (0.60, (232, 226, 242)), (1, (232, 226, 242))],
     (0.30, 0.42, 30, (255, 246, 216), (250, 210, 220), 180, 190), 0, (98, 118, 140), (204, 212, 222), 4, 0.52, 0.86, 0.05, MOUNTAINS),
    ([(0, (58, 92, 122)), (0.66, (232, 178, 168)), (1, (232, 178, 168))],
     (0.50, 0.62, 34, (255, 232, 212), (255, 200, 180), 200, 170), 0, (108, 58, 88), (172, 112, 132), 3, 0.66, 0.92, 0.03, DUNES),
    ([(0, (60, 24, 92)), (0.66, (236, 120, 100)), (1, (236, 120, 100))],
     (0.70, 0.62, 30, (255, 220, 178), (255, 170, 130), 180, 180), 0, (70, 28, 60), (172, 82, 122), 4, 0.60, 0.90, 0.045, MOUNTAINS),
    ([(0, (38, 118, 140)), (0.62, (202, 232, 226)), (1, (202, 232, 226))],
     (0.62, 0.38, 22, (255, 255, 248), (220, 245, 240), 150, 180), 0, (14, 58, 64), (122, 172, 172), 5, 0.50, 0.88, 0.045, MOUNTAINS),
    ([(0, (118, 168, 222)), (0.56, (236, 245, 251)), (1, (236, 245, 251))],
     None, 0, (78, 94, 116), (232, 238, 244), 4, 0.42, 0.84, 0.07, MOUNTAINS),
    ([(0, (242, 178, 118)), (0.60, (251, 226, 192)), (1, (251, 226, 192))],
     (0.36, 0.40, 28, (255, 248, 222), (255, 214, 160), 190, 190), 0, (88, 38, 22), (204, 122, 72), 5, 0.48, 0.88, 0.05, MOUNTAINS),
]
# The files' order: the gallery fills its 3 columns one after the other (4 photos each), so the first screen
# shows the first 3 of each column: blue, night, orange | desert, green, lavender | purple, teal, snow.
ORDER = [0, 3, 2, 5, 4, 1, 6, 7, 8, 9, 10, 11]


def draw_photo(index, spec):
    stops, the_sun, star_count, near, far, layers, top, bottom, amp, waves = spec
    rng = random.Random(2026 + index)
    img = sky(stops)
    if star_count:
        img = stars(img, rng, star_count, 0.62)
    if the_sun:
        img = sun(img, *the_sun)
    img = ridges(img, rng, near, far, layers, top=top, bottom=bottom, amp=amp, waves=waves)
    return img.convert("RGB").resize((W, H), Image.LANCZOS)


SYNC_PY = '''"""Sync a folder to the team share every night."""

import argparse
import shutil
from datetime import datetime
from pathlib import Path


def sync(source: Path, target: Path, *, dry_run: bool = False) -> int:
    """Copy files that are new or changed; return how many were copied."""
    copied = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        dest = target / path.relative_to(source)
        if dest.exists() and dest.stat().st_mtime >= path.stat().st_mtime:
            continue
        print(f"{'would copy' if dry_run else 'copy'} {path} -> {dest}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        copied += 1
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    count = sync(args.source, args.target, dry_run=args.dry_run)
    print(f"{datetime.now():%Y-%m-%d %H:%M} done, {count} file(s)")


if __name__ == "__main__":
    main()
'''

CODE = {
    "app.js": "// Show when the page was opened, and reload the list every minute.\n"
              "const opened = new Date();\n"
              "document.querySelector('#opened').textContent = opened.toLocaleString();\n"
              "setInterval(() => location.reload(), 60_000);\n",
    "config.toml": "# Where the nightly sync copies from and to.\n[sync]\nsource = \"/srv/projects\"\n"
                   "target = \"/mnt/team-share/projects\"\ndry_run = false\nkeep_days = 30\n",
    "deploy.sh": "#!/bin/sh\n# Build the site and copy it to the web server.\nset -eu\nnpm ci\nnpm run build\n"
                 "rsync -av --delete dist/ deploy@web1:/var/www/site/\n"
                 "echo \"Deployed $(git rev-parse --short HEAD) at $(date '+%Y-%m-%d %H:%M')\"\n",
    "index.html": "<!doctype html>\n<html lang=\"en\">\n<meta charset=\"utf-8\">\n<title>Team share</title>\n"
                  "<link rel=\"stylesheet\" href=\"style.css\">\n<h1>Team share</h1>\n"
                  "<p>Opened at <span id=\"opened\"></span></p>\n<script src=\"app.js\"></script>\n",
    "style.css": "body {\n  margin: 2rem auto;\n  max-width: 40rem;\n  font: 16px/1.6 system-ui, sans-serif;\n"
                 "  color: #1b1b1f;\n}\n\nh1 {\n  font-weight: 500;\n}\n",
    "sync.py": SYNC_PY,
}
CODE_TIMES = ["2026-11-02 09:12", "2026-10-28 16:40", "2026-11-05 21:03", "2026-10-28 16:35", "2026-10-30 11:20",
              "2026-11-09 10:30"]

LANG = {
    "zh": {
        "folders": {"code": "代码", "docs": "文档", "photos": "照片", "software": "软件"},
        "readme": "# 演示存储\n\n这里是 HFile 的演示内容，用来展示文件列表、画廊和预览。\n\n"
                  "- 照片：打开画廊模式，点一张图片放大看\n- 文档：Markdown 和文本的预览\n"
                  "- 代码：代码高亮，也可以在线编辑后保存\n- 软件：大文件的下载\n\n"
                  "> 文件夹里有 README.md 时，HFile 把它显示在文件列表的下面。\n",
        "docs": {
            "产品介绍.md": "# 产品介绍\n\n团队共享盘放着项目文件、设计稿和发布包。\n\n"
                         "## 目录\n\n- 代码：同步和部署脚本\n- 照片：活动和外出的照片\n- 软件：各个系统的安装包\n",
            "会议记录 2026-11-05.txt": "周会 2026-11-05\n\n1. 共享盘每晚同步，保留 30 天。\n"
                                      "2. 下周换新的部署脚本。\n3. 照片按月份整理。\n",
            "上线检查清单.md": "# 上线检查清单\n\n- [x] 备份数据库\n- [x] 跑完测试\n- [ ] 更新说明文档\n- [ ] 通知大家\n",
        },
    },
    "en": {
        "folders": {"code": "Code", "docs": "Documents", "photos": "Photos", "software": "Software"},
        "readme": "# Demo storage\n\nThis is HFile's demo content, for showing the file list, the gallery and previews.\n\n"
                  "- Photos: gallery view; click to enlarge\n"
                  "- Documents: previews of Markdown and text\n"
                  "- Code: highlighting; edit and save online\n"
                  "- Software: downloads of large files\n\n"
                  "> When a folder has a README.md, HFile shows it below the file list.\n",
        "docs": {
            "Product overview.md": "# Product overview\n\nThe team share holds project files, designs and releases.\n\n"
                                   "## Folders\n\n- Code: the sync and deploy scripts\n- Photos: from events and trips\n"
                                   "- Software: installers for each system\n",
            "Meeting notes 2026-11-05.txt": "Weekly meeting, 2026-11-05\n\n1. The share syncs every night and keeps 30 days.\n"
                                            "2. The new deploy script goes in next week.\n3. Photos get sorted by month.\n",
            "Launch checklist.md": "# Launch checklist\n\n- [x] Back up the database\n- [x] Run the tests\n"
                                   "- [ ] Update the docs\n- [ ] Tell everyone\n",
        },
    },
}
DOC_TIMES = ["2026-11-03 14:20", "2026-11-05 18:02", "2026-11-06 18:45"]
SOFTWARE = {"HFile-0.2.0-linux-x86_64.tar.gz": 41_877_504, "HFile-0.2.0-windows-x86_64.zip": 39_190_528}
SOFTWARE_TIMES = ["2026-11-12 19:38", "2026-11-12 19:40"]
ROOT_TIMES = {"code": "2026-11-09 10:30", "docs": "2026-11-06 18:45", "photos": "2026-10-20 23:10",
              "software": "2026-11-12 19:40", "README.md": "2026-11-10 17:00"}


def write(path, text, stamp):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.utime(path, (when(stamp), when(stamp)))


def build(out, lang, photos):
    texts = LANG[lang]
    root = os.path.join(out, f"content-{lang}")
    shutil.rmtree(root, ignore_errors=True)
    folders = {key: os.path.join(root, name) for key, name in texts["folders"].items()}
    for folder in folders.values():
        os.makedirs(folder)
    for (name, text), stamp in zip(CODE.items(), CODE_TIMES):
        write(os.path.join(folders["code"], name), text, stamp)
    for (name, text), stamp in zip(texts["docs"].items(), DOC_TIMES):
        write(os.path.join(folders["docs"], name), text, stamp)
    for i, (name, src) in enumerate(photos):
        dest = os.path.join(folders["photos"], name)
        shutil.copyfile(src, dest)
        stamp = when("2026-10-20 08:07") + i * 4500
        os.utime(dest, (stamp, stamp))
    for (name, size), stamp in zip(SOFTWARE.items(), SOFTWARE_TIMES):
        path = os.path.join(folders["software"], name)
        with open(path, "wb") as f:
            f.truncate(size)  # sparse: no disk space
        os.utime(path, (when(stamp), when(stamp)))
    write(os.path.join(root, "README.md"), texts["readme"], ROOT_TIMES["README.md"])
    for key, folder in folders.items():
        os.utime(folder, (when(ROOT_TIMES[key]), when(ROOT_TIMES[key])))
    return root


def main():
    out = sys.argv[1]
    drawn = os.path.join(out, "photos")
    os.makedirs(drawn, exist_ok=True)
    photos = []
    for position, index in enumerate(ORDER):
        name = f"IMG_{3101 + position}.jpg"
        path = os.path.join(drawn, name)
        draw_photo(index, PHOTOS[index]).save(path, quality=88, optimize=True)
        photos.append((name, path))
    for lang in ("zh", "en"):
        print(build(out, lang, photos))


if __name__ == "__main__":
    main()
