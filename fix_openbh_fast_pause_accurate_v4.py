#!/usr/bin/env python3
"""DreamTwo/OpenBH fast local-video PAUSE V4: no long-GOP rewind.

Patch only lib/service/servicemp3.cpp and servicemp3.h on the current amlogic
branch after MediaPlayer toggle V3. V4 preserves the opt-in flag:
    touch /tmp/openbh_fast_pause_flush  (fast pause, accurate resume)
    rm -f /tmp/openbh_fast_pause_flush  (normal slower pause)

In the V3 implementation, the optional PAUSE and V2 RESUME both use
FLUSH|KEY_UNIT seeking. KEY_UNIT can snap backward by a long GOP (10s+).
V4 uses the pipeline's absolute position (not audio-decoder time) and
FLUSH|ACCURATE for the fast-pause pair, retaining the pre-existing V2 resume
path for unmodified/normal pauses. Saved target is invalidated on explicit
seek/trickplay so the next resume cannot undo a user's seek.

No change to Python, remote keys, DVB, drivers, GStreamer plugins or feed.
NOT cross-compiled / hardware-tested. --check prints a diff without writing.
"""
from __future__ import annotations
import argparse
import difflib
from pathlib import Path
import re
import sys

CPP = Path('lib/service/servicemp3.cpp')
HDR = Path('lib/service/servicemp3.h')
MARK = 'OPENBH_LOCAL_FAST_PAUSE_V4'


def function_region(src, symbol):
    # eServiceMP3 has two overloaded pause() methods: pause() and
    # pause(ePtr<iPauseableService>&). Match the exact signature.
    signatures = {
        'pause': r'\(\s*\)',
        'unpause': r'\(\s*\)',
        'seekToImpl': r'\(\s*pts_t\s+to,\s*bool\s+accurate\s*\)',
        'trickSeek': r'\(\s*gdouble\s+ratio\s*\)',
    }
    signature = signatures.get(symbol)
    if not signature:
        raise RuntimeError(f'Unsupported function selector: {symbol}')
    matches = list(re.finditer(r'(?m)^RESULT\s+eServiceMP3::' + re.escape(symbol) + signature + r'\s*\{', src))
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one eServiceMP3::{symbol}(): {len(matches)}')
    b = src.index('{', matches[0].start())
    level = 0
    for p in range(b, len(src)):
        if src[p] == '{': level += 1
        elif src[p] == '}':
            level -= 1
            if level == 0: return matches[0].start(), p+1
    raise RuntimeError(f'Unterminated {symbol}')


def substitute_once(s, a, b, what):
    count = s.count(a)
    if count != 1:
        raise RuntimeError(f'Expected exactly one {what}, found {count}. No changes made.')
    return s.replace(a, b, 1)


def update_header(h):
    if MARK in h:
        if 'm_openbh_fast_pause_target_ns' not in h:
            raise RuntimeError('V4 marker in header but target member is missing')
        return h
    return substitute_once(h, '\tgint64 m_last_seek_pos;',
        '\tgint64 m_last_seek_pos;\n'
        '\t/* OPENBH_LOCAL_FAST_PAUSE_V4: absolute GStreamer position preserved\n'
        '\t * across the opt-in accurate PAUSE/RESUME. -1 means inactive. */\n'
        '\tgint64 m_openbh_fast_pause_target_ns = -1;', 'header m_last_seek_pos')


def update_cpp(src):
    if MARK in src:
        needed = ('GST_SEEK_FLAG_ACCURATE', 'm_openbh_fast_pause_target_ns',
                  '[OPENBH_LOCAL_FAST_PAUSE_V4]')
        if not all(n in src for n in needed):
            raise RuntimeError('V4 marker present but C++ changes appear incomplete')
        return src
    if 'OPENBH_LOCAL_FAST_PAUSE_V3' not in src or 'OPENBH_LOCAL_MOVIE_RESUME_V2' not in src:
        raise RuntimeError('Expected already-applied V2+V3 local-movie implementation')
    # Only adjust the V3 local video pause() region.
    a, b = function_region(src, 'pause')
    fn = src[a:b]
    fn = substitute_once(fn,
        '''\t\tconst bool fast_pause_test = g_file_test("/tmp/openbh_fast_pause_flush", G_FILE_TEST_EXISTS);
\t\tpts_t fast_pause_pts = 0;
\t\tconst bool fast_pause_have_pts = fast_pause_test && (getRawPlayPosition(fast_pause_pts) >= 0);''',
        '''\t\t/* OPENBH_LOCAL_FAST_PAUSE_V4: use the pipeline timeline in ns.
\t\t * getRawPlayPosition() may prefer audio decoder time, which can be in
\t\t * a different/stale PTS domain; FLUSH|KEY_UNIT can snap back a GOP. */
\t\tm_openbh_fast_pause_target_ns = -1;
\t\tconst bool fast_pause_test = g_file_test("/tmp/openbh_fast_pause_flush", G_FILE_TEST_EXISTS);
\t\tgint64 fast_pause_ns = -1;
\t\tconst bool fast_pause_have_pts = fast_pause_test &&
\t\t\tgst_element_query_position(m_gst_playbin, GST_FORMAT_TIME, &fast_pause_ns) &&
\t\t\tfast_pause_ns >= 0;''', 'V3 pause position capture')
    fn = substitute_once(fn,
        '''\t\tif (fast_pause_test)
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
\t\t}''',
        '''\t\tif (fast_pause_test)
\t\t{
\t\t\tbool fast_pause_flushed = false;
\t\t\tif (fast_pause_have_pts)
\t\t\t{
\t\t\t\tfast_pause_flushed = gst_element_seek(m_gst_playbin, 1.0, GST_FORMAT_TIME,
\t\t\t\t\t(GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_ACCURATE),
\t\t\t\t\tGST_SEEK_TYPE_SET, fast_pause_ns,
\t\t\t\t\tGST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);
\t\t\t\tif (fast_pause_flushed)
\t\t\t\t\tm_openbh_fast_pause_target_ns = fast_pause_ns;
\t\t\t}
\t\t\teDebug("[OPENBH_LOCAL_FAST_PAUSE_V4] pause flush=%d valid_pos=%d target_ms=%lld elapsed_ms=%lld",
\t\t\t\t(int)fast_pause_flushed, (int)fast_pause_have_pts,
\t\t\t\t(long long)(fast_pause_have_pts ? fast_pause_ns / GST_MSECOND : -1),
\t\t\t\t(long long)((g_get_monotonic_time() - pause_begin_us) / 1000));
\t\t}''', 'V3 pause flush block')
    src = src[:a] + fn + src[b:]
    # Preserve V2 for normal pauses; use saved exact pause target in fast path.
    a, b = function_region(src, 'unpause')
    fn = src[a:b]
    fn = substitute_once(fn,
        '''\t\tpts_t resume_pts = 0;
\t\tconst bool have_resume_pts = getRawPlayPosition(resume_pts) >= 0;''',
        '''\t\t/* OPENBH_LOCAL_FAST_PAUSE_V4: do not re-query a keyframe-snapped
\t\t * position after an accurate fast-pause flush. Remember the requested
\t\t * pause position until playback resumes or the user seeks elsewhere. */
\t\tconst bool accurate_resume = m_openbh_fast_pause_target_ns >= 0;
\t\tconst gint64 accurate_ns = m_openbh_fast_pause_target_ns;
\t\tpts_t resume_pts = 0;
\t\tconst bool have_resume_pts = accurate_resume || (getRawPlayPosition(resume_pts) >= 0);
\t\tif (accurate_resume)
\t\t\tresume_pts = (pts_t)(accurate_ns / 11111LL);''', 'V2 resume position query')
    fn = substitute_once(fn,
        '''\t\t\tconst gint64 resume_ns = (gint64)resume_pts * 11111LL;
\t\t\tflushed = gst_element_seek(m_gst_playbin, 1.0, GST_FORMAT_TIME,
\t\t\t\t(GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_KEY_UNIT),
\t\t\t\tGST_SEEK_TYPE_SET, resume_ns,
\t\t\t\tGST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);''',
        '''\t\t\tconst gint64 resume_ns = accurate_resume ? accurate_ns : (gint64)resume_pts * 11111LL;
\t\t\tconst GstSeekFlags seek_flags = (GstSeekFlags)(GST_SEEK_FLAG_FLUSH |
\t\t\t\t(accurate_resume ? GST_SEEK_FLAG_ACCURATE : GST_SEEK_FLAG_KEY_UNIT));
\t\t\tflushed = gst_element_seek(m_gst_playbin, 1.0, GST_FORMAT_TIME,
\t\t\t\tseek_flags,
\t\t\t\tGST_SEEK_TYPE_SET, resume_ns,
\t\t\t\tGST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);''', 'V2 resume flush seek')
    fn = substitute_once(fn,
        '''\t\tsetHDAudioAuxState(m_gst_playbin, GST_STATE_PLAYING);
\t\tm_prev_decoder_time = -1;''',
        '''\t\tsetHDAudioAuxState(m_gst_playbin, GST_STATE_PLAYING);
\t\teDebug("[OPENBH_LOCAL_FAST_PAUSE_V4] resume accurate=%d flush=%d target_ms=%lld",
\t\t\t(int)accurate_resume, (int)flushed,
\t\t\t(long long)(accurate_resume ? accurate_ns / GST_MSECOND : -1));
\t\tm_openbh_fast_pause_target_ns = -1;
\t\tm_prev_decoder_time = -1;''', 'V2 resume tail')
    src = src[:a] + fn + src[b:]
    # If the user seeks while paused, the cached pause target must not override it.
    a, b = function_region(src, 'seekToImpl')
    fn = src[a:b]
    fn = substitute_once(fn,
        '''\t/* convert pts to nanoseconds */\n''',
        '''\t/* Explicit seek supersedes the previous saved fast-pause point. */
\tm_openbh_fast_pause_target_ns = -1;
\t/* convert pts to nanoseconds */
''', 'seekToImpl explicit-seek invalidation')
    src = src[:a] + fn + src[b:]
    a, b = function_region(src, 'trickSeek')
    fn = src[a:b]
    fn = substitute_once(fn,
        '''\tif (!m_gst_playbin)
\t\treturn -1;''',
        '''\tif (!m_gst_playbin)
\t\treturn -1;
\t/* Trick play supersedes the previous saved fast-pause point. */
\tm_openbh_fast_pause_target_ns = -1;''', 'trickSeek explicit-trickplay invalidation')
    src = src[:a] + fn + src[b:]
    return src


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not CPP.is_file() or not HDR.is_file():
        raise RuntimeError('Run from root of Mike1250/enigma2 amlogic checkout')
    old = {p: p.read_text(encoding='utf-8') for p in (CPP, HDR)}
    new = {CPP: update_cpp(old[CPP]), HDR: update_header(old[HDR])}
    for p in (CPP, HDR):
        if old[p] == new[p]:
            print(f'{p}: V4 already applied')
        elif args.check:
            print(f'{p}: PATCH READY')
            sys.stdout.writelines(difflib.unified_diff(
                old[p].splitlines(True), new[p].splitlines(True),
                fromfile=f'{p} (before)', tofile=f'{p} (V4)', n=3))
        else:
            p.write_text(new[p], encoding='utf-8')
            print(f'{p}: updated')
    print('No Python, driver, subtitles or kernel changed. Review with git diff --check.')

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
