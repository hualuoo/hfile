"""A web server with directory listings, in the styles of nginx (HTML and "autoindex_format json"), Apache's
FancyIndexing table, lighttpd's mod_dirlisting, Caddy's browse and Python's http.server (tests only, no Django).

    fake = FakeAutoIndex(files, style="nginx").start()   # files: {"/a/b.txt": b"..."}
    fake.url + "/pub/"

Folders are the ones above the files, and `folders`. A folder's URL without its "/" redirects to it, as those
servers do; files answer GET with Range, and HEAD. The pages are written as the servers' sources write them:
nginx's ngx_http_autoindex_module.c (exact sizes after the time, or JSON), Apache's mod_autoindex.c (sizes as
apr_strfsize() gives them: bytes below 973, else rounded), lighttpd's mod_dirlisting.c (rounded sizes, the exact
one in the size cell's data-value), Caddy v2.10's browse.go and browse.html (JSON to a request whose Accept has
application/json, a folder's name with a "/" after it; else rows with the size in data-size and a <time>), Python's
http.server (only links).

Knobs: auth ((username, password) for HTTP Basic); head_status (files answer HEAD with it); precompressed (paths
of files that have a .gz beside them, sent to a request that accepts gzip and has no Range, as nginx's gzip_static
does; gzip_static "always" sends it whatever the request accepts); descriptions ({path: text}, Apache's
description column); absolute (hrefs are whole paths, escaped as IIS escapes them: only some characters); charset
(in the HTML pages' Content-Type); forbidden (folders that answer 403); caddy_json (False: Caddy answers HTML
whatever the Accept). To look at: requests (all of them), heads (HEAD requests of files), listing_requests ((Accept,
Accept-Encoding) of each listing request).
"""

from __future__ import annotations

import base64
import gzip
import json
import threading
import urllib.parse

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response

# The time of every entry, as each style writes it: nginx, Apache, an HTTP date (nginx's JSON, Last-Modified),
# lighttpd, Caddy's <time datetime> and its text.
STAMP = ("17-Nov-2026 12:00", "2026-11-17 12:00", "Tue, 17 Nov 2026 12:00:00 GMT", "2026-Nov-17 12:00:00",
         "2026-11-17T12:00:00Z", "11/17/2026 12:00:00 PM +00:00")


def apache_size(size: int) -> str:
    """APR's apr_strfsize(), which mod_autoindex shows."""
    if size < 973:
        return f"{size:3d} "
    units, unit = "KMGTPE", 0
    while True:
        remain = size & 1023
        size >>= 10
        if size >= 973:
            unit += 1
            continue
        if size < 9 or (size == 9 and remain < 973):
            remain = (remain * 5 + 256) // 512
            if remain >= 10:
                size, remain = size + 1, 0
            return f"{size}.{remain}{units[unit]}"
        if remain >= 512:
            size += 1
        return f"{size:3d}{units[unit]}"


def lighttpd_size(size: int) -> str:
    """mod_dirlisting.c's http_list_directory_sizefmt()."""
    if 0 < size < 100:
        size += 99
    unit = -1
    while True:
        remain = size & 1023
        size >>= 10
        unit += 1
        if not size & ~1023:
            break
    remain = min(remain // 100, 9)
    if size > 999:
        size, remain, unit = 0, 9, unit + 1
    return f"{size}.{remain}{'KMGTPE'[unit]}"


def iec_size(size: int) -> str:
    """Caddy's HumanSize(), go-humanize's IBytes()."""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


class FakeAutoIndex:
    def __init__(self, files: dict[str, bytes], *, folders: tuple[str, ...] = (), style: str = "nginx", prefix: str = "/pub"):
        self.lock = threading.Lock()
        self.files = dict(files)
        self.folders = {"/"} | set(folders)
        for path in self.files:
            parts = path.strip("/").split("/")[:-1]
            for n in range(1, len(parts) + 1):
                self.folders.add("/" + "/".join(parts[:n]))
        self.style = style
        self.prefix = prefix
        self.auth: tuple[str, str] | None = None
        self.head_status: int | None = None
        self.precompressed: set[str] = set()
        self.gzip_static = "on"
        self.descriptions: dict[str, str] = {}
        self.absolute = False
        self.charset: str | None = None
        self.forbidden: set[str] = set()
        self.caddy_json = True
        self.requests = 0
        self.heads = 0
        self.listing_requests: list[tuple[str | None, str | None]] = []
        self.server = FakeServer(self.handle)

    def start(self) -> FakeAutoIndex:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url + self.prefix + "/"

    def _children(self, folder: str) -> list[tuple[str, bool]]:
        prefix = "/" if folder == "/" else folder + "/"
        names = {(p[len(prefix):], p in self.folders) for p in list(self.files) + list(self.folders)
                 if p != folder and p.startswith(prefix) and "/" not in p[len(prefix):]}
        return sorted(names)

    def _href(self, folder: str, name: str, is_dir: bool) -> str:
        if self.absolute:  # as IIS writes them: the whole path, with fewer characters escaped than quote() does
            whole = f"{self.prefix}{folder.rstrip('/')}/{name}"
            return urllib.parse.quote(whole, safe="/()+!*',;=&$@~") + ("/" if is_dir else "")
        return urllib.parse.quote(name) + ("/" if is_dir else "")

    def _html(self, body: str) -> Response:
        kind = "text/html" + (f"; charset={self.charset}" if self.charset else "")
        return Response(200, body.encode("utf-8"), {"Content-Type": kind})

    def _page(self, folder: str, accept: str) -> Response:
        children = self._children(folder)
        sizes = {name: len(self.files[folder.rstrip("/") + "/" + name]) for name, is_dir in children if not is_dir}
        if self.style == "json":
            items = [{"name": name, "type": "directory" if is_dir else "file", "mtime": STAMP[2],
                      **({} if is_dir else {"size": sizes[name]})}
                     for name, is_dir in children]
            return Response(200, json.dumps(items).encode(), {"Content-Type": "application/json"})
        if self.style == "caddy" and self.caddy_json and "application/json" in accept.lower():
            items = [{"name": name + ("/" if is_dir else ""), "size": 4096 if is_dir else sizes[name],
                      "url": "./" + urllib.parse.quote(name) + ("/" if is_dir else ""), "mod_time": STAMP[4],
                      "mode": 2147484141 if is_dir else 420, "is_dir": is_dir, "is_symlink": False}
                     for name, is_dir in children]
            return Response(200, (json.dumps(items) + "\n").encode(), {"Content-Type": "application/json; charset=utf-8"})
        rows = []
        for name, is_dir in children:
            href = self._href(folder, name, is_dir)
            text = name.replace("&", "&amp;").replace("<", "&lt;")
            size = sizes.get(name)
            if self.style == "nginx":
                shown = text + ("/" if is_dir else "")
                shown = shown if len(shown) <= 50 else shown[:47] + "..&gt;"  # nginx cuts long names in the text
                rows.append(f'<a href="{href}">{shown}</a>{" " * 20}{STAMP[0]}{" " * 8}{"-" if is_dir else size}')
            elif self.style == "apache":
                description = self.descriptions.get(folder.rstrip("/") + "/" + name, "&nbsp;")
                rows.append(f'<tr><td valign="top">[ ]</td><td><a href="{href}">{text}{"/" if is_dir else ""}</a></td>\n'
                            f'<td align="right">{STAMP[1]}  </td>\n<td align="right">{"  - " if is_dir else apache_size(size)}</td>'
                            f'<td>{description}</td></tr>')
            elif self.style == "lighttpd":
                if is_dir:
                    rows.append(f'<tr class="d"><td class="n"><a href="{href}">{text}</a>/</td><td class="m">{STAMP[3]}</td>'
                                '<td class="s" data-value="-1">- &nbsp;</td><td class="t">Directory</td></tr>')
                else:
                    rows.append(f'<tr><td class="n"><a href="{href}">{text}</a></td><td class="m">{STAMP[3]}</td>'
                                f'<td class="s" data-value="{size}">{lighttpd_size(size)}</td>'
                                '<td class="t">application/octet-stream</td></tr>')
            elif self.style == "caddy":
                cell = ("<td>&mdash;</td>" if is_dir else
                        f'<td class="size" data-size="{size}">\n<div class="sizebar">\n<div class="sizebar-bar"></div>\n'
                        f'<div class="sizebar-text">\n{iec_size(size)}\n</div>\n</div>\n</td>')
                rows.append(f'<tr class="file">\n<td></td>\n<td>\n<a href="./{href}">\n<svg></svg>\n'
                            f'<span class="name">{text}{"/" if is_dir else ""}</span>\n</a>\n</td>\n{cell}\n'
                            f'<td class="timestamp hideable">\n<time datetime="{STAMP[4]}">{STAMP[5]}</time>\n</td>\n'
                            '<td class="hideable"></td>\n</tr>')
            else:  # Python's http.server
                rows.append(f'<li><a href="{href}">{text}{"/" if is_dir else ""}</a></li>')
        title = f"Index of {self.prefix}{folder}"
        if self.style == "nginx":
            return self._html(f'<html><head><title>{title}</title></head><body><h1>{title}</h1><hr><pre><a href="../">../</a>\n'
                              + "\n".join(rows) + "\n</pre><hr></body></html>")
        if self.style == "apache":
            return self._html(f"<html><body><h1>{title}</h1><table>\n<tr><th><a href=\"?C=N;O=D\">Name</a></th><th><a href=\"?C=M;O=A\">Last modified</a></th></tr>\n"
                              f'<tr><td></td><td><a href="{self.prefix}/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td></tr>\n'
                              + "\n".join(rows) + "\n</table></body></html>")
        if self.style == "lighttpd":
            return self._html(f'<html><body><h2>{title}</h2><div class="list"><table summary="Directory Listing"><thead><tr>'
                              '<th class="n">Name</th><th class="m">Last Modified</th><th class="s">Size</th><th class="t">Type</th>'
                              '</tr></thead><tbody>\n<tr class="d"><td class="n"><a href="../">..</a>/</td><td class="m" data-value="-1">'
                              '&nbsp;</td><td class="s" data-value="-1">- &nbsp;</td><td class="t">Directory</td></tr>\n'
                              + "\n".join(rows) + "\n</tbody></table></div></body></html>")
        if self.style == "caddy":
            return self._html('<html><body><main><table><thead><tr><th></th><th>Name</th><th>Size</th><th>Modified</th></tr>'
                              '</thead><tbody>\n<tr>\n<td></td>\n<td>\n<a href="..">\n<span class="go-up">Up</span>\n</a>\n</td>\n'
                              '</tr>\n' + "\n".join(rows) + "\n</tbody></table></main></body></html>")
        return self._html(f"<!DOCTYPE HTML><html><head><title>Directory listing for {folder}</title></head><body><ul>\n"
                          + "\n".join(rows) + "\n</ul></body></html>")

    def handle(self, req) -> Response:
        with self.lock:
            self.requests += 1
        if self.auth is not None:
            expected = "Basic " + base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
            if req.header("Authorization") != expected:
                raise HTTPError(401, b"Unauthorized", {"WWW-Authenticate": 'Basic realm="files"'})
        path = urllib.parse.unquote(req.path)
        if not path.startswith(self.prefix + "/"):
            raise HTTPError(404, b"Not Found")
        rest = path[len(self.prefix):]
        with self.lock:
            if rest.endswith("/"):
                folder = rest.rstrip("/") or "/"
                if folder not in self.folders:
                    raise HTTPError(404, b"Not Found")
                self.listing_requests.append((req.header("Accept"), req.header("Accept-Encoding")))
                if folder in self.forbidden:
                    raise HTTPError(403, b"<html><body>403 Forbidden</body></html>")
                return self._page(folder, req.header("Accept") or "")
            if rest in self.folders:
                return Response(301, b"", {"Location": req.path + "/"})
            data = self.files.get(rest)
            if data is not None and req.method == "HEAD":
                self.heads += 1
                if self.head_status is not None:
                    raise HTTPError(self.head_status, b"")
        if data is None:
            raise HTTPError(404, b"Not Found")
        accepts_gzip = "gzip" in (req.header("Accept-Encoding") or "") or self.gzip_static == "always"
        if rest in self.precompressed and accepts_gzip and not req.header("Range"):
            return Response(200, gzip.compress(data, mtime=0), {"Content-Type": "text/plain", "Content-Encoding": "gzip",
                                                                "Vary": "Accept-Encoding", "Last-Modified": STAMP[2]})
        return file_response(data, req.header("Range"), {"Last-Modified": STAMP[2]})
