"""Our NFO files: what Jellyfin shows for a title lives in them, locked, so Jellyfin shows the same after losing its
own database. Writing over an existing NFO keeps what is already there (corrections made by hand, elements we don't
know) and only adds what is missing, unless a field is named to be replaced."""

import os
import xml.etree.ElementTree as ET

ROOTS = {"movie": "movie", "tvshow": "tvshow", "season": "season", "episode": "episodedetails"}


def path_for(kind, path):
    """The NFO of a movie or series folder, a season folder, or an episode's video file."""
    if kind == "episode":
        return os.path.splitext(path)[0] + ".nfo"
    return os.path.join(path, {"movie": "movie.nfo", "tvshow": "tvshow.nfo", "season": "season.nfo"}[kind])


def element(tag, text=None, *children, **attributes):
    """An element with its text, child elements and attributes; numbers are written as text."""
    e = ET.Element(tag, {k: str(v) for k, v in attributes.items()})
    if text is not None:
        e.text = str(text)
    e.extend(children)
    return e


def read(path):
    """The root element of an NFO, or None if there is none. A broken one raises ET.ParseError, so it is never
    written over by accident."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return ET.fromstring(f.read())
    except FileNotFoundError:
        return None


def _empty(e):
    return not (e.text or "").strip() and len(e) == 0


def _key(e):
    if e.tag == "actor":
        return (e.findtext("name") or "").strip().casefold()
    if e.tag == "uniqueid":
        return (e.get("type") or "").casefold()
    return None


def merge(existing, new, replace=(), actor_replace=()):
    """Adds the elements of `new` to `existing`: a tag in `replace` or missing (or empty) in `existing` is taken from
    `new`; an existing list stays as it is, except that its actors get the children they lack (tmdbid, thumb) from the
    actor of the same name (those in `actor_replace` even when they have them), and ids of a type it has none of are
    added. `lockdata` is always true."""
    by_tag: dict[str, list[ET.Element]] = {}
    for e in new:
        if e.tag != "lockdata":
            by_tag.setdefault(e.tag, []).append(e)
    for tag, elements in by_tag.items():
        current = existing.findall(tag)
        if tag in replace or all(_empty(e) for e in current):
            for e in current:
                existing.remove(e)
            existing.extend(elements)
        elif tag in ("actor", "uniqueid"):
            known = {_key(e): e for e in current}
            for e in elements:
                match = known.get(_key(e))
                if match is None:
                    if tag == "uniqueid":
                        existing.append(e)
                    continue
                for child in e:
                    if child.tag in actor_replace or all(_empty(old) for old in match.findall(child.tag)):
                        for old in match.findall(child.tag):
                            match.remove(old)
                        match.append(child)
    for e in existing.findall("lockdata"):
        existing.remove(e)
    existing.append(element("lockdata", "true"))
    return existing


def serialize(root):
    ET.indent(root, "  ")
    return b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def write(nfo, root, before=None):
    """Writes root to the file unless it serializes to `before` (the file's content as read); True if written."""
    after = serialize(root)
    if after == before:
        return False
    temporary = nfo + ".tmp"
    with open(temporary, "wb") as f:
        f.write(after)
    os.replace(temporary, nfo)
    return True


def save(kind, path, new, replace=(), actor_replace=(), file=None):
    """Merges `new` (a list of elements) into the NFO of `path` (see path_for; `file` names another one, such as a
    movie's <video>.nfo), creating it if there is none, and writes it only if it changed. True if it was written."""
    nfo = file or path_for(kind, path)
    existing = read(nfo)
    before = None if existing is None else serialize(existing)
    root = merge(existing if existing is not None else ET.Element(ROOTS[kind]), new, replace, actor_replace)
    return write(nfo, root, before)
