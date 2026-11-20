#!/usr/bin/env python3
"""Builds apps/web/static/web/icons.svg, the site's icon sprite, from Material Symbols Rounded (weight 400,
grade 0, 24 px; Apache License 2.0), downloaded from github.com/google/material-design-icons.

    python scripts/icons.py [--cache 文件夹]

Add a name to ICONS (or FILLED, for a destination of the navigation, whose selected item shows the
filled form, "-fill") and run it again. Templates use {% icon "name" %}, scripts App.icon("name");
apps/web/tests.py checks that every name they use is in the sprite. Only the path of each SVG is kept.
"""

import argparse
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPRITE = ROOT / "apps" / "web" / "static" / "web" / "icons.svg"
URL = "https://raw.githubusercontent.com/google/material-design-icons/master/symbols/web/{name}/materialsymbolsrounded/{name}{fill}_24px.svg"

ICONS = """
    folder folder_open draft description image movie music_note audio_file picture_as_pdf code article
    table_chart slideshow folder_zip
    upload upload_file drive_folder_upload download link share qr_code_2 content_copy drive_file_move
    drive_file_rename_outline delete create_new_folder
    search sort grid_view view_list filter_list tune
    more_vert more_horiz menu close check arrow_back arrow_forward arrow_upward arrow_downward chevron_left
    chevron_right expand_more expand_less home open_in_new drag_indicator
    settings storage cloud dns person group lock lock_open key visibility visibility_off login logout link_off
    admin_panel_settings shield badge language policy receipt_long history upgrade backup restore
    account_circle password
    light_mode dark_mode contrast palette
    refresh info warning error help schedule check_circle cancel
    play_arrow pause skip_next skip_previous volume_up fullscreen zoom_in zoom_out rotate_right
    add remove edit save
    block troubleshoot campaign
    network_check toggle_on toggle_off science
    docs terminal menu_book view_in_ar photo_library cloud_upload file_copy vertical_align_top folder_off
    add_link dataset_linked
    rotate_left fit_width play_circle subtitles shuffle repeat repeat_one queue_music preview
    delete_sweep casino location_on
""".split()
# Names the site uses for a symbol that Material Symbols calls otherwise (Material Icons' old names).
ALIASES = {"restore": "settings_backup_restore"}
# The filled form too ("<name>-fill"): what the navigation and the /manage/ menu show for the current item.
FILLED = """
    home folder settings admin_panel_settings storage group person upgrade palette share link receipt_long
    shield cloud key lock history dns policy badge language backup info description
    tune block troubleshoot
    add_link download location_on
""".split()

_PATH_DATA = re.compile(r"[0-9MmLlHhVvCcSsQqTtAaZz .,\-eE]+")


def fetch(name, fill, cache):
    """The path data and view box of one symbol, from the cache folder or from GitHub."""
    name = ALIASES.get(name, name)
    file_name = f"{name}{'_fill1' if fill else ''}_24px.svg"
    cached = cache / file_name if cache else None
    if cached and cached.exists():
        data = cached.read_bytes()
    else:
        url = URL.format(name=name, fill="_fill1" if fill else "")
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read()
        if cached:
            cached.write_bytes(data)
    root = ET.fromstring(data)  # Google's own files; only two attributes are taken from them
    paths = [element.get("d", "") for element in root.iter() if element.tag.endswith("path")]
    if len(paths) != 1 or not _PATH_DATA.fullmatch(paths[0]):
        raise SystemExit(f"{file_name}: not one plain path")
    # Most use the 960 grid (viewBox="0 -960 960 960"); a few older ones are 24 by 24 without a viewBox.
    view_box = root.get("viewBox") or f"0 0 {root.get('width', '')} {root.get('height', '')}"
    if not re.fullmatch(r"[0-9 .\-]+", view_box):
        raise SystemExit(f"{file_name}: odd viewBox {view_box!r}")
    return view_box, paths[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, help="keep the downloaded SVG files here, and use them next time")
    args = parser.parse_args()
    if args.cache:
        args.cache.mkdir(parents=True, exist_ok=True)
    missing = sorted(set(FILLED) - set(ICONS))
    if missing:
        raise SystemExit(f"FILLED names not in ICONS: {missing}")
    wanted = [(name, False) for name in ICONS] + [(name, True) for name in FILLED]
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda item: fetch(*item, args.cache), wanted))
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg">',
        "  <!-- The site's icons: Material Symbols Rounded, weight 400 (Apache License 2.0,",
        "       https://github.com/google/material-design-icons). Made by scripts/icons.py; don't edit by hand.",
        '       Used as <svg class="icon"><use href="…/icons.svg#i-NAME"></use></svg>, through the {% icon %}',
        '       template tag or App.icon(). "-fill" ones are the filled form, for the current destination. -->',
    ]
    for (name, fill), (view_box, path) in zip(wanted, found):
        lines.append(f'  <symbol id="i-{name}{"-fill" if fill else ""}" viewBox="{view_box}"><path d="{path}"/></symbol>')
    lines.append("</svg>")
    SPRITE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{SPRITE}: {len(wanted)} icons, {SPRITE.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
