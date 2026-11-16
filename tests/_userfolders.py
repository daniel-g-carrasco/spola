# SPDX-License-Identifier: GPL-3.0-or-later
"""The guard of the user's own folders: no test writes where the app keeps
the user's files (its settings, state, data and cache: %APPDATA%\\<Exe> and
%LOCALAPPDATA%\\<Exe> on Windows, the XDG folders elsewhere).

The app's tests/_setup.py does two things with it, in this order and as
early as it can:

    _userfolders.watch(SLUG, EXE_NAME)      # the user's own folders, as the environment names them now
    _userfolders.isolate()                  # then the tests get folders of their own

isolate() makes one temporary folder and points the four XDG variables
(config, state, data, cache) into it: app/paths.py lets them win on every
system, so whatever asks the app for its folders during the tests (the
settings, the window's state, the log, a cache) gets a folder that is
thrown away at the exit. All four, always: with XDG_CONFIG_HOME alone the
state, the data and the cache stay under the real ~/.local and ~/.cache,
on Windows too. The toolkit reads the same variables, so it starts with
its own defaults (no theme of the user's, no settings of the desktop).

watch() is called before isolate() and before any test changes the
environment (HOME, APPDATA), since the folders watched are worked out
from the environment as it is at that moment. It takes the list of
files and folders there, with their size and time of change (never their
content: a folder may hold gigabytes of models), and at the exit of the
process takes it again. What is new, changed or gone is written on stderr,
and the process ends with code 1 even if every test passed.

How the run is made to fail. unittest.main() decides the exit code and
raises SystemExit; the handlers of atexit run after that, and what they
raise or return no longer changes the code. So the handler, when it finds
a write, flushes stdout and stderr and ends the process with os._exit(1):
the one way to change the code from there. The handlers run last in,
first out, and watch() is called at the top of _setup.py: those registered
after it (the temporary folders of the tests) have already run. os._exit
skips only those registered before it, and only on that failing path. A
runner of its own (tools/run_tests.py) asks check() at the end, prints
message() and decides the code itself (failing()): the handler then does
nothing.

Another process may write there too: the app itself, open while the tests
run, saves its settings or its window. The guard cannot tell who wrote, so
it lists every change, says that the app may have been open, and fails.
With <SLUG>_TESTS_USER_FOLDERS=warn in the environment (the slug in
capitals, "-" as "_", the name switch_name() gives) it lists them all the
same and does not fail: for a run made on purpose with the app open. The
guard never deletes nor puts back anything.

On Windows the XDG folders under the user's home are watched as well:
app/paths.py lets the XDG variables win on Windows (for the tests), so a
test that sets XDG_CONFIG_HOME alone puts the state and the data under the
real ~/.local. A process started by multiprocessing watches nothing: the
parent does.
"""

import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ENV_SUFFIX = "_TESTS_USER_FOLDERS"
WARN = "warn"
# the variables app/paths.py reads, and the folder each gets inside the tests' temporary one
XDG = (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
       ("XDG_CACHE_HOME", "cache"))

_names: tuple[str, str] | None = None
_temporary: Path | None = None
_watched: tuple[Path, ...] | None = None
_before: dict = {}
_checked: list = []
_switch = ""
_warn_only = False


def switch_name(slug: str) -> str:
    """The environment variable that turns the guard into a warning:
    "my-app" gives MY_APP_TESTS_USER_FOLDERS."""
    return re.sub(r"[^A-Za-z0-9]", "_", slug).upper() + ENV_SUFFIX


def _paths_class():
    import _setup
    return _setup.shared("app.paths").Paths


def _home(environ) -> Path:
    return Path(environ.get("HOME") or environ.get("USERPROFILE") or os.path.expanduser("~"))


def roots(slug: str, exe_name: str, environ=None) -> list[Path]:
    """The user's own folders of this app for an environment (the real one
    by default, as it is now), sorted, none inside another."""
    environ = dict(os.environ if environ is None else environ)
    paths_class = _paths_class()
    layouts = []
    if os.name == "nt":
        plain = {k: v for k, v in environ.items() if not k.startswith("XDG_")}
        layouts.append(paths_class.from_env(slug, exe_name, plain))
        xdg = dict(environ)
        if not (xdg.get("XDG_CONFIG_HOME") and os.path.isabs(xdg["XDG_CONFIG_HOME"])):
            xdg["XDG_CONFIG_HOME"] = str(_home(environ) / ".config")
        xdg.setdefault("HOME", str(_home(environ)))
        layouts.append(paths_class.from_env(slug, exe_name, xdg))
    else:
        layouts.append(paths_class.from_env(slug, exe_name, environ))
    found = set()
    for paths in layouts:
        found |= {Path(p) for p in (paths.config, paths.state, paths.data, paths.cache) if p is not None}
        if os.name == "nt" and paths.state.parent.name == exe_name:
            found.add(paths.state.parent)       # %LOCALAPPDATA%\<Exe>, its own files too
    ordered = sorted(found)
    return [r for r in ordered if not any(o != r and o in r.parents for o in ordered)]


def snapshot(folders) -> dict[str, tuple]:
    """Every file and folder under `folders`, the folders themselves
    included: (size, time of change in ns) for a file, ("dir",) for a
    folder. A folder that does not exist gives nothing; a file or a folder
    that cannot be read is left out, and the rest is still taken. Links are
    not followed."""
    out = {}
    stack = [str(f) for f in folders]
    while stack:
        folder = stack.pop()
        try:
            if not os.path.isdir(folder) or os.path.islink(folder):
                continue
            out[folder] = ("dir",)
            with os.scandir(folder) as entries:
                for entry in entries:
                    # joined here, not entry.path: under MSYS2 with MSYSTEM set Python writes "/" and
                    # scandir a backslash, and the same file would have two spellings
                    path = os.path.join(folder, entry.name)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                        else:
                            st = entry.stat(follow_symlinks=False)
                            out[path] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def changes(before: dict, after: dict) -> list[str]:
    """What differs between two snapshots: a line "new <path>", "changed
    <path>" or "gone <path>" for each, sorted by path."""
    lines = [(p, "new") for p in after if p not in before]
    lines += [(p, "changed") for p in after if p in before and after[p] != before[p]]
    lines += [(p, "gone") for p in before if p not in after]
    return [f"{what} {path}" for path, what in sorted(lines)]


def watching() -> tuple[Path, ...] | None:
    """The folders watch() is watching in this process, or None if it was
    never called (or this is a child of multiprocessing)."""
    return _watched


def check() -> list[str]:
    """What the tests have written in the user's own folders since watch():
    for a runner that sets the exit code itself. Once asked, the check at
    the exit is not made again."""
    _checked.append(True)
    if _watched is None:
        return []
    return changes(_before, snapshot(_watched))


def report(written: list[str], switch: str, failing: bool) -> str:
    head = ("FAILED: the tests wrote in the user's own folders:" if failing
            else "WARNING: the tests wrote in the user's own folders (not failed, " + switch + "=" + WARN + "):")
    return (head + "\n  " + "\n  ".join(written)
            + "\nA test must use temporary folders (Paths built on a temporary directory, an environ that points "
            + "there, or the folders isolate() gives to the whole run). If the app itself was open during the run, its own writes are listed too: run again with it "
            + "closed, or set " + switch + "=" + WARN + " to be told without failing. Nothing was deleted or put "
            + "back.\n")


def failing() -> bool:
    """Whether a write fails the run (not with <SLUG>_TESTS_USER_FOLDERS=warn)."""
    return not _warn_only


def message(written: list[str]) -> str:
    """What to print for the writes check() gave: for a runner of its own."""
    return report(written, _switch, failing())


def _at_exit() -> None:
    if _checked:
        return
    written = check()
    if not written:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass                                   # a closed or missing stream: the message below still goes
    sys.stderr.write(message(written))
    sys.stderr.flush()
    if failing():
        os._exit(1)


def _child() -> bool:
    """A process started by multiprocessing: its parent watches, and its
    environment already names the parent's temporary folders."""
    multiprocessing = sys.modules.get("multiprocessing")
    return multiprocessing is not None and multiprocessing.parent_process() is not None


def isolate(environ=None) -> Path | None:
    """Give the tests folders of their own: one temporary folder, made
    once and removed at the exit, with the four XDG variables of `environ`
    (the real environment by default) pointing into it. Whatever the
    variables were, they are replaced: a user who sets XDG_STATE_HOME must
    not have the tests write there. Returns the temporary folder (None in
    a child of multiprocessing, which keeps its parent's)."""
    global _temporary
    if _child():
        return None
    if _temporary is None:
        _temporary = Path(tempfile.mkdtemp(prefix="tests-home-"))
        atexit.register(shutil.rmtree, str(_temporary), ignore_errors=True)
    environ = os.environ if environ is None else environ
    for variable, name in XDG:
        environ[variable] = str(_temporary / name)
    return _temporary


def in_use(environ=None) -> list[Path]:
    """The folders the app given to watch() asks for in an environment
    (the real one by default, as it is now): after isolate(), the tests'
    own."""
    if _names is None:
        return []
    paths = _paths_class().from_env(*_names, os.environ if environ is None else environ)
    return [Path(p) for p in (paths.config, paths.state, paths.data, paths.cache_folder)]


def watch(slug: str, exe_name: str, environ=None) -> tuple[Path, ...] | None:
    """Take the user's own folders of this app now and check them at the
    exit of the process (see the module's docstring). A second call keeps
    the first one's folders. Returns the folders watched (None in a child
    of multiprocessing)."""
    global _names, _watched, _before, _switch, _warn_only
    if _watched is not None:
        return _watched
    if _child():
        return None
    _names = (slug, exe_name)
    environ = dict(os.environ if environ is None else environ)
    _switch = switch_name(slug)
    _warn_only = environ.get(_switch, "").strip().lower() == WARN
    _watched = tuple(roots(slug, exe_name, environ))
    _before = snapshot(_watched)
    atexit.register(_at_exit)
    return _watched
