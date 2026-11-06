"""Everything that touches the disk safely: path confinement, atomic writes, line endings.

Every write the server does goes through `confine` (the path must stay inside the vault
and must not pass through a symlink or junction) and `atomic_write` / `StagedWrite`
(temp file in the same directory, fsync, os.replace). Directories whose name starts
with a dot (.git, .obsidian, .trash) are never read or written through user paths.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass


class VaultError(Exception):
    """An expected, user-actionable failure. The MCP layer shows its message to the agent."""


_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def key(s: str) -> str:
    """Comparison key for names and paths: NFC + casefold (case-insensitive, macOS-safe)."""
    return unicodedata.normalize("NFC", s).casefold()


def clean_rel(rel: str, *, what: str = "path") -> str:
    """Validate a vault-relative path typed by a user/agent and return it with '/' separators.

    Rejects absolute paths, '..', hidden components (.git, .obsidian, ...), and names
    that are illegal on Windows (so a vault stays portable between machines).
    """
    s = rel.replace("\\", "/").strip()
    if not s:
        raise VaultError(f"{what} is empty")
    if s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        raise VaultError(f"{what} {rel!r} is absolute; use a path relative to the vault root, like 'Projects/Plan.md'")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts:
        raise VaultError(f"{what} {rel!r} is empty")
    for p in parts:
        if p == "..":
            raise VaultError(f"{what} {rel!r} contains '..'; paths must stay inside the vault")
        if p.startswith("."):
            raise VaultError(f"{what} {rel!r}: names starting with '.' (like .git or .obsidian) are off limits")
        if _BAD_CHARS.search(p):
            raise VaultError(f"{what} {rel!r}: {p!r} contains a character not allowed in file names (< > : \" | ? * or control characters)")
        if p != p.rstrip(" ."):
            raise VaultError(f"{what} {rel!r}: {p!r} ends with a space or dot, which Windows does not allow")
        if p.split(".")[0].casefold() in _RESERVED:
            raise VaultError(f"{what} {rel!r}: {p!r} is a reserved device name on Windows")
        if len(p.encode("utf-8")) > 255:
            raise VaultError(f"{what} {rel!r}: a name component is longer than 255 bytes")
    return "/".join(parts)


def confine(root: str, rel: str) -> str:
    """Absolute OS path for vault-relative `rel`, guaranteed inside `root` (a realpath).

    Raises VaultError if the path escapes the vault, including through a symlink or a
    Windows junction anywhere in the chain (even one that points back inside the vault:
    refusing all links is simpler to reason about than allowing some).
    """
    parts = clean_rel(rel).split("/")
    target = os.path.join(root, *parts)
    real = os.path.realpath(target)
    expected = os.path.normcase(target)
    if os.path.normcase(real) != expected:
        # realpath resolved something: a link, or a case/8.3 difference. Distinguish.
        inside = _is_inside(root, real)
        if not inside:
            raise VaultError(f"{rel!r} resolves outside the vault (symbolic link or junction); refusing")
        cur = root
        for p in parts:
            cur = os.path.join(cur, p)
            if os.path.islink(cur) or _is_junction(cur):
                raise VaultError(f"{rel!r} passes through a symbolic link or junction ({p!r}); refusing")
    return target


def _is_junction(p: str) -> bool:
    isj = getattr(os.path, "isjunction", None)
    return bool(isj(p)) if isj else False


def _is_inside(root: str, path: str) -> bool:
    r = os.path.normcase(os.path.abspath(root))
    p = os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath([r, p]) == r
    except ValueError:  # different drives on Windows
        return False


def detect_eol(text: str) -> str:
    """The line ending a file mostly uses: '\\r\\n' if CRLF outnumbers bare LF, else '\\n'."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def to_eol(text: str, eol: str) -> str:
    """Convert every line ending in `text` to `eol`."""
    return text.replace("\r\n", "\n").replace("\n", eol)


@dataclass
class FileText:
    text: str
    bom: bool

    def encode(self) -> bytes:
        return (b"\xef\xbb\xbf" if self.bom else b"") + self.text.encode("utf-8")


def read_text(path: str, shown: str | None = None) -> FileText:
    """Read a note as UTF-8 without any newline translation (CRLF stays CRLF)."""
    with open(path, "rb") as f:
        raw = f.read()
    return decode(raw, shown or path)


def decode(raw: bytes, shown: str) -> FileText:
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    try:
        return FileText(raw.decode("utf-8"), bom)
    except UnicodeDecodeError as e:
        raise VaultError(f"{shown} is not valid UTF-8 text ({e.reason} at byte {e.start}); not touching it") from e


class StagedWrite:
    """A fully written temp file waiting to be moved into place with `commit`."""

    def __init__(self, path: str, data: bytes) -> None:
        self.path = path
        d = os.path.dirname(path)
        fd, self.tmp = tempfile.mkstemp(prefix=".mdvault-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(path):
                import shutil

                shutil.copymode(path, self.tmp)
            else:
                os.chmod(self.tmp, 0o644)
        except BaseException:
            self.discard()
            raise

    def commit(self) -> None:
        try:
            os.replace(self.tmp, self.path)
        except BaseException:
            self.discard()
            raise

    def discard(self) -> None:
        try:
            os.unlink(self.tmp)
        except FileNotFoundError:
            pass


def atomic_write(path: str, data: bytes) -> None:
    """Write `data` to `path` atomically (temp file in the same directory + os.replace)."""
    StagedWrite(path, data).commit()


def make_parent_dirs(root: str, rel: str) -> None:
    """Create the directories above vault-relative `rel`, re-checking confinement."""
    parent = rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel else ""
    if parent:
        confine(root, parent)  # raises if any existing part is a link or escapes
        os.makedirs(os.path.join(root, *parent.split("/")), exist_ok=True)
