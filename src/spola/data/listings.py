"""Read the listings that rclone bisync writes after every successful run.

They are bisync's memory of the last state both sides agreed on, so Spola
uses them as its own source of truth ("synced") instead of keeping a second
database that could drift. The format is internal to rclone; the version of
rclone shipped in the Flatpak is pinned and tests/test_listings.py fails if a
new rclone changes it.

    # bisync listing v1 from 2026-10-10T12:52:41.648420224+0000
    -        4 md5:5bbf5a52... - 2026-10-10T12:52:41.508231189+0000 "f1.txt"
    d       -1 - - 2026-10-10T12:52:41.510466191+0000 "sub"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADER = "# bisync listing v1"

_LINE = re.compile(r'^([-d])\s+(-?\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(".*")$')


@dataclass(frozen=True)
class Entry:
    path: str
    is_dir: bool
    size: int
    hash: str      # "md5:..." or "-"
    modtime: str


def go_unquote(text: str) -> str:
    """Undo Go's strconv.Quote, which rclone uses for the path column."""
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise ValueError(f"not a quoted string: {text!r}")
    body, out, i = text[1:-1], bytearray(), 0
    simple = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34, "'": 39}
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8", errors="surrogateescape")
            i += 1
            continue
        esc = body[i + 1]
        if esc in simple:
            out.append(simple[esc])
            i += 2
        elif esc == "x":
            out.append(int(body[i + 2:i + 4], 16))
            i += 4
        elif esc in "uU":
            n = 4 if esc == "u" else 8
            out += chr(int(body[i + 2:i + 2 + n], 16)).encode("utf-8")
            i += 2 + n
        elif esc in "01234567":
            out.append(int(body[i + 1:i + 4], 8))
            i += 4
        else:
            raise ValueError(f"unknown escape \\{esc}")
    return out.decode("utf-8", errors="surrogateescape")


def parse(text: str) -> list[Entry]:
    lines = text.splitlines()
    if not lines or not lines[0].startswith(HEADER):
        raise ValueError("not a bisync listing (or an unknown version)")
    entries = []
    for line in lines[1:]:
        if not line.strip():
            continue
        m = _LINE.match(line)
        if not m:
            raise ValueError(f"unexpected listing line: {line!r}")
        flag, size, hash_, _id, modtime, quoted = m.groups()
        entries.append(Entry(go_unquote(quoted), flag == "d", int(size), hash_, modtime))
    return entries


def find(workdir: Path, side: int) -> Path | None:
    """The current listing of side 1 (local) or 2 (Koofr) in a pair's workdir."""
    matches = sorted(workdir.glob(f"*.path{side}.lst"))
    return matches[0] if len(matches) == 1 else None


def load(workdir: Path, side: int) -> list[Entry] | None:
    path = find(workdir, side)
    if path is None:
        return None
    return parse(path.read_text(encoding="utf-8", errors="surrogateescape"))


def counts(workdir: Path) -> tuple[int, int] | None:
    """Entries of the last run's listing on each side (files and folders:
    what bisync's deletion check divides by), or None without them."""
    found = []
    for side in (1, 2):
        path = find(workdir, side)
        if path is None:
            return None
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            found.append(sum(1 for line in f if line[:1] in ("-", "d")))
    return found[0], found[1]


def count(workdir: Path) -> int | None:
    """Entries agreed on by the last run (the smaller side), or None."""
    both = counts(workdir)
    return min(both) if both is not None else None


_EXACT = re.compile(r'^([-d])(\s+)(-?\d+) (\S+) (\S+) (\S+) (".*")$')


def retime(path: Path, times: dict[str, str]) -> int:
    """Give the entries of a listing new modification times ({relative
    path: bisync's time text}), every other byte as it was. Each line must
    rebuild exactly before anything is written: a listing in a format this
    does not know is left alone (ValueError). Returns how many changed."""
    # newline="": the bytes as they are, both ways (in text mode Windows
    # writes "\r\n", and bisync then reads an empty listing)
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
    lines = text.split("\n")
    if not lines or not lines[0].startswith(HEADER):
        raise ValueError("not a bisync listing (or an unknown version)")
    out, changed = [lines[0]], 0
    for line in lines[1:]:
        if not line:
            out.append(line)
            continue
        m = _EXACT.match(line)
        if not m or "{}{}{} {} {} {} {}".format(*m.groups()) != line:
            raise ValueError(f"unexpected listing line: {line!r}")
        flag, gap, size, hash_, id_, modtime, quoted = m.groups()
        new = times.get(go_unquote(quoted))
        if new is not None and new != modtime:
            changed += 1
            modtime = new
        out.append(f"{flag}{gap}{size} {hash_} {id_} {modtime} {quoted}")
    if changed:
        tmp = path.with_name(path.name + ".retime")
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write("\n".join(out))
        tmp.replace(path)
    return changed


_GO_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def go_quote(text: str) -> str:
    """A Go string literal that go_unquote, and Go's strconv.Unquote, read
    back as `text`: not byte for byte what strconv.Quote writes (which also
    escapes what it does not consider printable), but bisync only reads it."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch in _GO_ESCAPES:
            out.append(_GO_ESCAPES[ch])
        elif 0xDC80 <= code <= 0xDCFF:       # a byte that is not UTF-8, as surrogateescape keeps it
            out.append(f"\\x{code - 0xDC00:02x}")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _renamed(path: Path, moves: list[tuple[str, str]]) -> tuple[str, int] | None:
    """The listing at `path` with every entry at or under a move's old
    folder at its new place, and how many moved; None when none did. Every
    line must rebuild exactly first, as in retime."""
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
    lines = text.split("\n")
    if not lines or not lines[0].startswith(HEADER):
        raise ValueError("not a bisync listing (or an unknown version)")
    entries, moved = [], 0
    for line in lines[1:]:
        if not line:
            continue
        m = _EXACT.match(line)
        if not m or "{}{}{} {} {} {} {}".format(*m.groups()) != line:
            raise ValueError(f"unexpected listing line: {line!r}")
        flag, gap, size, hash_, id_, modtime, quoted = m.groups()
        name = go_unquote(quoted)
        for old, new in moves:
            if name == old or name.startswith(old + "/"):
                name = new + name[len(old):]
                if go_unquote(go_quote(name)) != name:
                    raise ValueError(f"a name this cannot write: {name!r}")
                line = f"{flag}{gap}{size} {hash_} {id_} {modtime} {go_quote(name)}"
                moved += 1
                break
        entries.append((name.encode("utf-8", errors="surrogateescape"), line))
    if not moved:
        return None
    entries.sort(key=lambda entry: entry[0])        # as bisync writes them: by name, byte by byte
    return "\n".join([lines[0], *(line for _name, line in entries), ""]), moved


def rename(first: Path, second: Path, here: list[tuple[str, str]], koofr: list[tuple[str, str]]) -> int:
    """A folder moved on both sides, written in both listings of the last
    synced state ({old folder: new folder} as each side spells them): what
    it held is where it is now, with the sizes, dates and checksums it had,
    so that bisync finds nothing to do for it. Both listings are prepared
    before either is replaced; a listing this does not understand leaves
    both alone (ValueError). Returns how many entries moved, both sides."""
    prepared = [(path, _renamed(path, moves)) for path, moves in ((first, here), (second, koofr))]
    total = 0
    written = []
    for path, result in prepared:
        if result is None:
            continue
        text, moved = result
        tmp = path.with_name(path.name + ".moved")
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        written.append((tmp, path))
        total += moved
    for tmp, path in written:
        tmp.replace(path)
    return total


_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?([+-]\d{4}|Z)$")


def modtime_ns(stamp: str) -> int | None:
    """A listing's time ("2026-10-15T09:00:59.704779200+0000") in
    nanoseconds since the epoch, as os.stat gives it; None if unreadable."""
    from datetime import datetime
    m = _TIME.match(stamp)
    if not m:
        return None
    whole, fraction, zone = m.groups()
    offset = "+00:00" if zone == "Z" else f"{zone[:3]}:{zone[3:]}"
    seconds = int(datetime.fromisoformat(whole + offset).timestamp())
    return seconds * 10**9 + int((fraction or "")[:9].ljust(9, "0"))


def bisync_time(stamp: str) -> str:
    """An RFC 3339 time (rclone lsjson's ModTime) as bisync writes it in a
    listing: UTC, nine decimals, "+0000"."""
    from datetime import datetime, timezone
    whole = re.sub(r"\.\d+", "", stamp).replace("Z", "+00:00")      # the decimals apart, all of them
    t = datetime.fromisoformat(whole)
    fraction = stamp.split(".", 1)[1] if "." in stamp else ""
    digits = re.match(r"\d*", fraction).group(0)[:9].ljust(9, "0")      # rclone writes up to nine
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + digits + "+0000"
