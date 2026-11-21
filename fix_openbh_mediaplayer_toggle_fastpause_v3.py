#!/usr/bin/env python3
"""OpenBH amlogic DreamTwo local MediaPlayer pause toggle + optional fast pause.

Run from root of Mike1250/enigma2 (amlogic):
    python3 fix_openbh_mediaplayer_toggle_fastpause_v3.py --check
    python3 fix_openbh_mediaplayer_toggle_fastpause_v3.py --apply

The MediaPlayer KEY_PLAYPAUSE action maps to MediaPlayer.pauseEntry(), not the
MoviePlayer InfoBarSeek handler. Correct its unconditional pause into a toggle.

Also adds an experimental LOCAL-VIDEO-ONLY GStreamer pause-flush option, disabled
by default. Enable at runtime with `touch /tmp/openbh_fast_pause_flush` on STB.
Disable with `rm -f /tmp/openbh_fast_pause_flush` (no reboot). The experiment
flushes the pipeline at the pause position after requesting PAUSED; it can
cause keyframe jumps or extra preroll, so compare and leave off if worse.

No driver/codec changes. The original local-movie resume V2 remains untouched.
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import re
import sys

PLUGIN = Path('lib/python/Plugins/Extensions/MediaPlayer/plugin.py')
CPP = Path('lib/service/servicemp3.cpp')
TOGGLE_MARKER = 'OPENBH_MEDIAPLAYER_TOGGLE_V3'
PAUSE_MARKER = 'OPENBH_LOCAL_FAST_PAUSE_V3'

OLD_PAUSE_ENTRY = '''\tdef pauseEntry(self):
\t\tself.pauseService()
\t\tif self.seekstate == self.SEEK_STATE_PAUSE:
\t\t\tself.show()
\t\telse:
\t\t\tself.hideAndInfoBar()
'''
NEW_PAUSE_ENTRY = '''\tdef pauseEntry(self):
\t\t# OPENBH_MEDIAPLAYER_TOGGLE_V3: KEY_PLAYPAUSE must toggle, not pause twice.
\t\tif self.seekstate == self.SEEK_STATE_PAUSE:
\t\t\tprint("[OPENBH_MEDIAPLAYER_TOGGLE_V3] RESUME")
\t\t\tself.unPauseService()
\t\t\tself.hideAndInfoBar()
\t\telse:
\t\t\tprint("[OPENBH_MEDIAPLAYER_TOGGLE_V3] PAUSE")
\t\t\tself.pauseService()
\t\t\tself.show()
'''

# Minimal source fragment from the previously installed V2 local-movie code.
PAUSE_BEGIN = '''\t\tconst gint64 pause_begin_us = g_get_monotonic_time();
\t\teDebug("[OPENBH_LOCAL_MOVIE_RESUME_V2] local movie PAUSE requested");
\t\tGstStateChangeReturn result = gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED);
'''
PAUSE_BEGIN_NEW = '''\t\t/* OPENBH_LOCAL_FAST_PAUSE_V3: opt-in test for the extra 1-2 seconds of
\t\t * decoded/queued video that may keep displaying after PAUSED is requested.
\t\t * Capture the actual playback position BEFORE pausing. A flushing
\t\t * seek while paused may clear queues, but may also move to a keyframe;
\t\t * keep it OFF by default and enable only when comparing. */
\t\tconst bool fast_pause_test = g_file_test("/tmp/openbh_fast_pause_flush", G_FILE_TEST_EXISTS);
\t\tpts_t fast_pause_pts = 0;
\t\tconst bool fast_pause_have_pts = fast_pause_test && (getRawPlayPosition(fast_pause_pts) >= 0);
\t\tconst gint64 pause_begin_us = g_get_monotonic_time();
\t\teDebug("[OPENBH_LOCAL_MOVIE_RESUME_V2] local movie PAUSE requested");
\t\tGstStateChangeReturn result = gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED);
'''
PAUSE_POST = '''\t\tsetHDAudioAuxState(m_gst_playbin, GST_STATE_PAUSED);
'''
PAUSE_POST_NEW = '''\t\tif (fast_pause_test)
\t\t{
\t\t\tbool fast_pause_flushed = false;
\t\t\tif (fast_pause_have_pts)
\t\t\t{
\t\t\t\tconst gint64 fast_pause_ns = (gint64)fast_pause_pts * 11111LL;
\t\t\t\tfast_pause_flushed = gst_element_seek(m_gst_playbin, 1.0, GST_FORMAT_TIME,
\t\t\t\t\t(GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_KEY_UNIT),
\t\t\t\t\tGST_SEEK_TYPE_SET, fast_pause_ns,
\t\t\t\t\tGST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);
\t\t\t}
\t\t\teDebug("[OPENBH_LOCAL_FAST_PAUSE_V3] flush=%d valid_pts=%d pts=%lld elapsed_ms=%lld",
\t\t\t\t(int)fast_pause_flushed, (int)fast_pause_have_pts,
\t\t\t\t(long long)fast_pause_pts,
\t\t\t\t(long long)((g_get_monotonic_time() - pause_begin_us) / 1000));
\t\t}
\t\tsetHDAudioAuxState(m_gst_playbin, GST_STATE_PAUSED);
'''


def bracket_region(s: str, needle: str) -> tuple[int, int]:
    hits = list(re.finditer(re.escape(needle), s))
    if len(hits) != 1:
        raise RuntimeError(f'Expected one {needle!r}; found {len(hits)}')
    start = hits[0].start()
    opening = s.find('{', hits[0].end())
    if opening < 0:
        raise RuntimeError(f'Brace not found for {needle}')
    depth = 0
    for i in range(opening, len(s)):
        if s[i] == '{':
            depth += 1
        elif s[i] == '}':
            depth -= 1
            if not depth:
                return start, i + 1
    raise RuntimeError(f'Function {needle} not terminated')


def edit_plugin(text: str) -> str:
    if TOGGLE_MARKER in text:
        if NEW_PAUSE_ENTRY not in text:
            raise RuntimeError('MediaPlayer toggle marker present but code differs; review manually')
        return text
    if text.count(OLD_PAUSE_ENTRY) != 1:
        raise RuntimeError('Unexpected MediaPlayer pauseEntry implementation; refusing overwrite')
    return text.replace(OLD_PAUSE_ENTRY, NEW_PAUSE_ENTRY, 1)


def edit_cpp(text: str) -> str:
    if PAUSE_MARKER in text:
        if PAUSE_BEGIN_NEW not in text or PAUSE_POST_NEW not in text:
            raise RuntimeError('C++ fast pause marker present but implementation differs; review manually')
        return text
    if 'OPENBH_LOCAL_MOVIE_RESUME_V2' not in text:
        raise RuntimeError('Expected previous local video resume V2 source is missing; refusing C++ edit')
    begin, end = bracket_region(text, 'RESULT eServiceMP3::pause()')
    func = text[begin:end]
    if func.count(PAUSE_BEGIN) != 1 or func.count(PAUSE_POST) != 1:
        raise RuntimeError('Unexpected local movie pause V2 block; refusing C++ edit')
    # Limit both replacements to pause(), not similar lines in other services.
    func = func.replace(PAUSE_BEGIN, PAUSE_BEGIN_NEW, 1)
    func = func.replace(PAUSE_POST, PAUSE_POST_NEW, 1)
    return text[:begin] + func + text[end:]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Preview changes, write nothing')
    mode.add_argument('--apply', action='store_true', help='Apply after validation')
    args = ap.parse_args()
    for path in (PLUGIN, CPP):
        if not path.is_file():
            raise RuntimeError(f'Missing {path}. Run from root of amlogic Enigma2 checkout.')

    originals = {p: p.read_text(encoding='utf-8') for p in (PLUGIN, CPP)}
    updated = {PLUGIN: edit_plugin(originals[PLUGIN]), CPP: edit_cpp(originals[CPP])}
    # Validate both transformations completely BEFORE modifying either file.
    compile(updated[PLUGIN], str(PLUGIN), 'exec')
    if 'RESULT eServiceMP3::unpause()' not in updated[CPP]:
        raise RuntimeError('Unexpected C++ source: unpause missing')
    for p in (PLUGIN, CPP):
        old, new = originals[p], updated[p]
        print(f'{p}: {"unchanged" if old == new else "patch ready"}')
        if args.check and old != new:
            diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=str(p) + '.before', tofile=str(p) + '.after', n=3)
            sys.stdout.writelines(diff)
    if args.apply:
        # Skip edits for already applied files. No automatic commits or pushes.
        for p in (PLUGIN, CPP):
            if originals[p] != updated[p]:
                p.write_text(updated[p], encoding='utf-8')
                print('Updated:', p)
        print('Run: git diff --check && git diff --stat')
        print('Fast-pause mode defaults OFF; to test it: touch /tmp/openbh_fast_pause_flush (on DreamTwo).')
    else:
        print('Preview only. Run --apply to modify the source files.')
    print('No driver/kernel/ko/subtitle/keymap files modified. Not yet compiled or device-tested.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, SyntaxError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
