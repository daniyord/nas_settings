# Bulgarian subtitles from subs.sab.bz (searched by IMDb id) and subsunacs.net (by title, kept when its cover
# carries the IMDb id). Neither has an API: these read their search pages, so a change to either site's HTML
# shows up as no results rather than an error.
import html
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# Between two requests to the same site
PAUSE = 3
SUBTITLE_EXTS = (".srt", ".sub", ".ass", ".ssa")
ARCHIVE_EXTS = (".zip", ".rar", ".7z")
MAX_PAGES = 8
last_request = {}


def fetch(url, data=None, referer=None):
    host = urllib.parse.urlsplit(url).netloc
    wait = last_request.get(host, 0) + PAUSE - time.time()
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    # Both sites are windows-1251 throughout, their forms too
    body = urllib.parse.urlencode(data, encoding="cp1251", errors="ignore").encode() if data is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=60) as response:
            disposition = response.headers.get("Content-Disposition", "")
            return response.read(), disposition
    finally:
        last_request[host] = time.time()


def text(raw):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def numbers(title):
    """(season, episode) a listing's title names: "02x09" -> (2, 9), "Season 2" or "- 02 -" -> (2, None)."""
    m = re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?:season|сезон)\s*(\d{1,2})(?!\d)|\s-\s(\d{1,2})\s-(?:\s|$)", title, re.I)
    if m:
        return int(m.group(1) or m.group(2)), None
    return None, None


def sab_search(imdb):
    found, url = [], f"http://subs.sab.bz/index.php?act=search&movie=&select-language=2&upldr=&yr=&release=&imdb={imdb}"
    for page in range(MAX_PAGES):
        raw, _ = fetch(url + (f"&st={page * 25}" if page else ""))
        page_html = raw.decode("cp1251", errors="replace")
        rows = re.findall(r'<tr class="subs-row">.*?</tr>', page_html, re.S)
        for row in rows:
            link = re.search(r'attach_id=(\d+)', row)
            title = re.search(r'onMouseout="hideddrivetip\(\)">(.*?)</a>\s*\((\d{4})\)', row, re.S)
            cells = [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            row_imdb = re.search(r"imdb\.com/title/(tt\d+)", row)
            if not (link and title) or (row_imdb and row_imdb.group(1) != imdb):
                continue
            info = re.search(r"Доп\. инфо</b>:(.*?)'\s*,\s*'#", html.unescape(row), re.S)
            name = text(title.group(1))
            found.append({"site": "sab", "id": link.group(1), "title": name, "year": int(title.group(2)),
                          "numbers": numbers(name), "fps": cell_float(cells, 7), "downloads": cell_int(cells, 10),
                          "info": text(info.group(1)) if info else "",
                          "url": f"http://subs.sab.bz/index.php?act=download&attach_id={link.group(1)}"})
        if len(rows) < 25:
            break
    return found


def unacs_search(title, year, imdb):
    raw, _ = fetch("https://subsunacs.net/search.php", {"m": title, "l": "0", "c": "", "y": str(year or 0), "a": "", "d": "", "u": "", "g": "", "t": "Submit", "imdbcheck": "1"},
                   referer="https://subsunacs.net/search.php")
    page_html = raw.decode("cp1251", errors="replace")
    found, wanted = [], imdb[2:].lstrip("0")
    for row in re.findall(r'<tr[^>]*>\s*<td class="tdMovie">.*?</tr>', page_html, re.S):
        link = re.search(r'<a href="(/subtitles/[^"]*?-(\d+)/)"[^>]*title="([^"]*)"[^>]*>(.*?)</a>(?:<span class="smGray">&nbsp;\((\d{4})\))?', row, re.S)
        if not link:
            continue
        cover = re.search(r"/ii/\w+/0*(\d+)\.jpg", html.unescape(link.group(3)))
        # Kept only when the listing names the same IMDb title; one without a cover can't be told apart
        if not cover or cover.group(1) != wanted:
            continue
        cells = [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        info = re.search(r"Инфо: </b><br>(.*)", html.unescape(link.group(3)), re.S)
        name = text(link.group(4))
        found.append({"site": "unacs", "id": link.group(2), "title": name, "year": int(link.group(5)) if link.group(5) else None,
                      "numbers": numbers(name), "fps": cell_float(cells, 2), "downloads": cell_int(cells, 6),
                      "info": text(info.group(1)) if info else "", "url": "https://subsunacs.net" + link.group(1)})
    return found


def cell_float(cells, i):
    try:
        return float(cells[i].replace(",", "."))
    except (IndexError, ValueError):
        return None


def cell_int(cells, i):
    try:
        return int(re.sub(r"\D", "", cells[i]))
    except (IndexError, ValueError):
        return 0


def files(candidate, cache_dir):
    """[(name, bytes)] of every subtitle file in a listing, from a cached copy after the first time."""
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, f"{candidate['site']}-{candidate['id']}")
    if not os.path.exists(cached):
        # From its search page, subsunacs gives a listing's archive rather than its page
        referer = "http://subs.sab.bz/" if candidate["site"] == "sab" else "https://subsunacs.net/search.php"
        raw, disposition = fetch(candidate["url"], referer=referer)
        name = re.search(r'filename="?([^";]+)', disposition)
        with open(cached + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(cached + ".tmp", cached)
        with open(cached + ".name", "w") as f:
            f.write(name.group(1) if name else "subtitle.zip")
    name = open(cached + ".name").read() if os.path.exists(cached + ".name") else "subtitle.zip"
    return unpack(name, open(cached, "rb").read())


def unpack(name, raw, depth=0):
    if not name.lower().endswith(ARCHIVE_EXTS) and raw[:4] not in (b"PK\x03\x04", b"Rar!") and raw[:6] != b"7z\xbc\xaf\x27\x1c":
        return [(name, raw)] if name.lower().endswith(SUBTITLE_EXTS) else []
    if depth > 2:
        return []
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "archive")
        with open(archive, "wb") as f:
            f.write(raw)
        target = os.path.join(tmp, "x")
        os.mkdir(target)
        # libarchive reads zip, rar (also RAR5) and 7z; it doesn't write outside target
        subprocess.run(["bsdtar", "-xf", archive, "-C", target], capture_output=True, timeout=120)
        for root, _, names in os.walk(target):
            for n in sorted(names):
                path = os.path.join(root, n)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                out += unpack(n, data, depth + 1)
    return out
