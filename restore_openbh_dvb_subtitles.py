#!/usr/bin/env python3
"""Restore pre-clean OpenBH DVB-channel subtitle handling, without touching kernel drivers.

Run inside Mike1250/enigma2 (amlogic) working tree:
    python3 restore_openbh_dvb_subtitles.py

Reads only from the historical Git backup. A clean working tree for the six
impacted files is required. Does not commit, push, or compile anything.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

BACKUP_BRANCH = "backup/amlogic-before-openbh-clean-20261116-103525"
BACKUP_SHA = "95855b55e267d1865ea150061f8ce4d12864307f"
FULL_RESTORE = ("lib/dvb/subtitle.cpp", "lib/dvb/subtitle.h")
PORT_ONLY = (
    "lib/dvb/idemux.h",
    "lib/dvb/demux.h",
    "lib/dvb/demux.cpp",
    "lib/service/servicedvb.cpp",
)
TARGETS = FULL_RESTORE + PORT_ONLY


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def git_text(*args: str) -> str:
    return git(*args).stdout.decode("utf-8")


def ensure(cond: bool, message: str) -> None:
    if not cond:
        raise RuntimeError(message)


def old_file(path: str) -> str:
    return git_text("show", f"{BACKUP_SHA}:{path}")


def old_function(src: str, signature: str) -> str:
    """Take one complete C++ function by its unique signature and balanced body."""
    positions = [m.start() for m in re.finditer(re.escape(signature), src)]
    ensure(len(positions) == 1, f"Expected one function: {signature}, found {len(positions)}")
    start = positions[0]
    brace = src.find("{", start + len(signature))
    ensure(brace != -1, f"Missing function body: {signature}")
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise RuntimeError(f"Unbalanced C++ function: {signature}")


def replace_function(current: str, previous: str, signature: str) -> str:
    present_fn = old_function(current, signature)
    backup_fn = old_function(previous, signature)
    ensure(current.count(present_fn) == 1, f"Ambiguous current function: {signature}")
    return current.replace(present_fn, backup_fn, 1)


def replace_between(current: str, previous: str, begin: str, end: str) -> str:
    for name, content in (("current", current), ("backup", previous)):
        ensure(content.count(begin) == 1, f"{name}: missing/ambiguous {begin}")
        ensure(content.count(end) == 1, f"{name}: missing/ambiguous {end}")
    old_part = previous[previous.index(begin):previous.index(end)]
    return current[:current.index(begin)] + old_part + current[current.index(end):]


def restore_idemux(current: str, previous: str) -> str:
    # Userspace interface only. NOT the kernel demux driver.
    declaration = "virtual RESULT startSubtitle(int pid) { return start(pid); }"
    ensure(declaration in previous, "Pre-clean iDVBPESReader interface not found")
    if declaration in current:
        return current
    anchor = "virtual RESULT start(int pid)=0;"
    ensure(current.count(anchor) == 1, "Cannot locate iDVBPESReader::start")
    return current.replace(anchor, anchor + "\n\t// DVB subtitle PES reader; all other readers retain ordinary start().\n\t" + declaration, 1)


def restore_demux_header(current: str, previous: str) -> str:
    ensure("RESULT startPES(int pid, bool subtitle);" in previous, "Backup lacks private PES entrypoint")
    ensure("RESULT startSubtitle(int pid);" in previous, "Backup lacks subtitle PES entrypoint")
    start = current.find("class eDVBPESReader:")
    end = current.find("};", start)
    ensure(start >= 0 and end > start, "Cannot locate eDVBPESReader class")
    section = current[start:end]
    ensure(section.count("void data(int);") == 1, "eDVBPESReader::data declaration missing")
    ensure(section.count("RESULT start(int pid);") == 1, "eDVBPESReader::start declaration missing")
    if "RESULT startPES(int pid, bool subtitle);" not in section:
        section = section.replace("void data(int);", "void data(int);\n\tRESULT startPES(int pid, bool subtitle);", 1)
    if "RESULT startSubtitle(int pid);" not in section:
        section = section.replace("RESULT start(int pid);", "RESULT start(int pid);\n\tRESULT startSubtitle(int pid);", 1)
    return current[:start] + section + current[end:]


def restore_demux_cpp(current: str, previous: str) -> str:
    # Enigma2 user-space PES reader: large PES reads + subtitle PES type.
    result = replace_function(current, previous, "void eDVBPESReader::data(int)")
    result = replace_function(result, previous, "eDVBPESReader::eDVBPESReader(")
    # Includes original start(), startSubtitle(), startPES() as a matched block.
    result = replace_between(result, previous,
                             "RESULT eDVBPESReader::start(int pid)",
                             "RESULT eDVBPESReader::stop()")
    return result


def restore_service_cpp(current: str, previous: str) -> str:
    # Only the pre-clean live DVB subtitle clock; preserve other player fixes.
    first = "static long long openbhSubtitlePtsDifference("
    second = "static bool openbhSubtitlePresentedVideoPTS("
    fn1 = old_function(previous, first)
    fn2 = old_function(previous, second)
    if first not in current:
        anchor = "bool m_is_streamx = false;"
        ensure(current.count(anchor) == 1, "Cannot place subtitle timing helpers")
        current = current.replace(anchor, anchor + "\n\n/* Restored pre-clean live DVB subtitle clock. */\n" + fn1 + "\n" + fn2 + "\n", 1)
    else:
        ensure(second in current, "Incomplete subtitle helpers already present")
    return replace_function(current, previous, "void eDVBServicePlay::checkSubtitleTiming()")


def main() -> None:
    root = pathlib.Path(git_text("rev-parse", "--show-toplevel").strip())
    branch = git_text("branch", "--show-current").strip()
    ensure(branch == "amlogic", f"Expected amlogic branch, currently on {branch!r}")
    ensure((root / "lib/dvb/subtitle.cpp").is_file(), "Not an Enigma2 source checkout")
    # If the historical SHA is not yet local, fetch precisely the Git backup branch.
    if git("cat-file", "-e", f"{BACKUP_SHA}^{{commit}}", check=False).returncode != 0:
        print("Fetching preserved pre-clean branch from origin ...")
        git("fetch", "origin", f"refs/heads/{BACKUP_BRANCH}:refs/remotes/origin/{BACKUP_BRANCH}")
    ensure(git_text("rev-parse", f"{BACKUP_SHA}^{{commit}}").strip() == BACKUP_SHA,
           "Historic backup commit verification failed")
    for path in TARGETS:
        ensure((root / path).is_file(), f"Missing current file {path}")
        ensure(git("diff", "--quiet", "--", path, check=False).returncode == 0,
               f"Uncommitted changes in {path}; commit/stash them before running")
        ensure(git("diff", "--cached", "--quiet", "--", path, check=False).returncode == 0,
               f"Staged changes in {path}; commit/stash them before running")

    output = {}
    for path in TARGETS:
        current = (root / path).read_text(encoding="utf-8")
        previous = old_file(path)
        if path in FULL_RESTORE:
            content = previous
        elif path.endswith("idemux.h"):
            content = restore_idemux(current, previous)
        elif path.endswith("demux.h"):
            content = restore_demux_header(current, previous)
        elif path.endswith("demux.cpp"):
            content = restore_demux_cpp(current, previous)
        elif path.endswith("servicedvb.cpp"):
            content = restore_service_cpp(current, previous)
        else:
            raise AssertionError(path)
        output[path] = content

    # All validation/extraction runs before writing ANY file.
    changed = []
    for path, content in output.items():
        p = root / path
        if p.read_text(encoding="utf-8") != content:
            p.write_text(content, encoding="utf-8")
            changed.append(path)

    print("\nPre-clean DVB subtitle userspace restore complete.")
    print("Changed: " + (", ".join(changed) if changed else "nothing; already restored"))
    print("No kernel/driver/ko/media_modules_aml files were touched.")
    print("\nReview changes:")
    print("  git diff --check")
    print("  git diff --stat")
    print("  git diff -- lib/dvb/subtitle.cpp lib/dvb/subtitle.h lib/dvb/idemux.h lib/dvb/demux.h lib/dvb/demux.cpp lib/service/servicedvb.cpp")
    print("To undo BEFORE committing:")
    print("  git restore -- " + " ".join(TARGETS))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError, UnicodeError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            sys.stderr.write(exc.stderr.decode("utf-8", errors="replace"))
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
