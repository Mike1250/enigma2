#!/usr/bin/env python3
"""OpenBH DreamTwo local GStreamer movie resume V2.

Run at the root of the Mike1250/enigma2 amlogic checkout:
    python3 /path/to/fix_openbh_local_video_resume_v2.py

Replaces ONLY the local-movie fast-path in eServiceMP3::unpause().
Leaves V1's MoviePlayer key handling and pause implementation intact, apart
from non-blocking pause diagnostic logging. No DVB/driver/media_modules edits.

Rationale: A bare PAUSED -> PLAYING transition may not re-preroll Amlogic
native video sink. A flush/key-unit seek from the paused position follows the
existing seekRelative/trickSeek recovery path that works with the right arrow.

WARNING: not cross-compiled or tested on DreamTwo; review diff and test.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

TARGET = Path('lib/service/servicemp3.cpp')
V1 = 'OPENBH_LOCAL_MOVIE_PAUSE_RESUME_V1'
V2 = 'OPENBH_LOCAL_MOVIE_RESUME_V2'


def locate_function(src: str, name: str) -> tuple[int, int]:
    pattern = re.compile(r'(?m)^RESULT\s+eServiceMP3::' + re.escape(name) + r'\(\)\s*\n\{')
    found = list(pattern.finditer(src))
    if len(found) != 1:
        raise RuntimeError(f'Expected one eServiceMP3::{name}(), got {len(found)}')
    start = src.index('{', found[0].start())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return start, i
    raise RuntimeError('Unterminated ' + name)


def locate_local_branch(src: str, name: str, expected_log: str) -> tuple[int, int]:
    first, last = locate_function(src, name)
    func = src[first:last]
    # Pin to the original V1 branch rather than any unrelated GStreamer path.
    marker = '\tif (openbh_local_movie && m_currentTrickRatio == 1.0)\n\t{\n\t\teDebug("[' + V1 + '] local movie ' + expected_log
    occurrences = [m.start() for m in re.finditer(re.escape(marker), func)]
    if len(occurrences) != 1:
        raise RuntimeError(f'Expected V1 local {name} branch exactly once; got {len(occurrences)}')
    absolute_start = first + occurrences[0]
    brace = src.index('{', absolute_start)
    depth = 0
    for i in range(brace, last):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return absolute_start, i + 1
    raise RuntimeError('Unterminated local branch in ' + name)


NEW_UNPAUSE = '''\tif (openbh_local_movie && m_currentTrickRatio == 1.0)
\t{
\t\t/* [OPENBH_LOCAL_MOVIE_RESUME_V2] Restore decoding with the same
\t\t * FLUSH + KEY_UNIT seek mechanism used by right-arrow seeking.
\t\t * A state-only PAUSED -> PLAYING transition is not sufficient on
\t\t * some Amlogic native video sinks after a buffered pause.
\t\t * The seek targets the CURRENT paused position, not a skip ahead. */
\t\tpts_t resume_pts = 0;
\t\tconst bool have_resume_pts = getRawPlayPosition(resume_pts) >= 0;
\t\tconst gint64 begin_us = g_get_monotonic_time();
\t\tGstState before = GST_STATE_NULL, pending = GST_STATE_VOID_PENDING;
\t\tconst GstStateChangeReturn prior =
\t\t\tgst_element_get_state(m_gst_playbin, &before, &pending, 0);
\t\teDebug("[OPENBH_LOCAL_MOVIE_RESUME_V2] resume requested: state=%s pending=%s status=%s pts=%lld valid=%d",
\t\t\tgst_element_state_get_name(before), gst_element_state_get_name(pending),
\t\t\tgst_element_state_change_return_get_name(prior),
\t\t\t(long long)resume_pts, (int)have_resume_pts);

\t\t/* Match the existing trickSeek recovery sequence: PLAYING first,
\t\t * then a flushing key-unit seek. Never wait for a blocking state
\t\t * transition from the Enigma2 UI/event thread. */
\t\tconst GstStateChangeReturn result =
\t\t\tgst_element_set_state(m_gst_playbin, GST_STATE_PLAYING);
\t\tif (result == GST_STATE_CHANGE_FAILURE)
\t\t{
\t\t\teWarning("[OPENBH_LOCAL_MOVIE_RESUME_V2] GST_STATE_PLAYING failed");
\t\t\treturn -1;
\t\t}

\t\tbool flushed = false;
\t\tif (have_resume_pts)
\t\t{
\t\t\tconst gint64 resume_ns = (gint64)resume_pts * 11111LL;
\t\t\tflushed = gst_element_seek(m_gst_playbin, 1.0, GST_FORMAT_TIME,
\t\t\t\t(GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_KEY_UNIT),
\t\t\t\tGST_SEEK_TYPE_SET, resume_ns,
\t\t\t\tGST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);
\t\t\tif (flushed && getHDAudioAuxState(m_gst_playbin))
\t\t\t\tseekHDAudioAuxPersistent(m_gst_playbin, resume_ns);
\t\t}
\t\tif (!flushed)
\t\t\teWarning("[OPENBH_LOCAL_MOVIE_RESUME_V2] resume flush not applied (position/seek unavailable)");

\t\tsetHDAudioAuxState(m_gst_playbin, GST_STATE_PLAYING);
\t\tm_prev_decoder_time = -1;
\t\tm_decoder_time_valid_state = 0;
\t\teDebug("[OPENBH_LOCAL_MOVIE_RESUME_V2] resume result=%s flush=%d elapsed_ms=%lld",
\t\t\tgst_element_state_change_return_get_name(result), (int)flushed,
\t\t\t(long long)((g_get_monotonic_time() - begin_us) / 1000));
\t\treturn flushed ? 0 : -1;
\t}'''


def transform(src: str) -> str:
    if V2 in src:
        return src
    if V1 not in src:
        raise RuntimeError('V1 source is not present. Apply the previous fix first or review this checkout manually.')

    start, end = locate_local_branch(src, 'unpause', 'unpause: GST_STATE_PLAYING')
    old = src[start:end]
    required = ['GST_STATE_PLAYING', 'setHDAudioAuxState', 'return 0;']
    if not all(k in old for k in required):
        raise RuntimeError('Unexpected V1 unpause block, refusing to replace it')
    src = src[:start] + NEW_UNPAUSE + src[end:]

    # Pause timing diagnostics ONLY (no change to pause semantics).
    # These tell us whether the 1-2s lag is inside gst_element_set_state,
    # or merely buffered frames being displayed after it returns.
    pstart, pend = locate_local_branch(src, 'pause', 'pause: GST_STATE_PAUSED')
    block = src[pstart:pend]
    trigger = f'\t\teDebug("[{V1}] local movie pause: GST_STATE_PAUSED");'
    if block.count(trigger) != 1:
        raise RuntimeError('Could not find the V1 pause log marker')
    block = block.replace(trigger,
        '\t\tconst gint64 pause_begin_us = g_get_monotonic_time();\n'
        f'\t\teDebug("[{V2}] local movie PAUSE requested");', 1)
    target = 'GstStateChangeReturn result = gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED);'
    if block.count(target) != 1:
        raise RuntimeError('Could not find the V1 pause state transition')
    block = block.replace(target, target + '\n'
        f'\t\teDebug("[{V2}] pause set_state result=%s elapsed_ms=%lld",\n'
        '\t\t\tgst_element_state_change_return_get_name(result),\n'
        '\t\t\t(long long)((g_get_monotonic_time() - pause_begin_us) / 1000));', 1)
    src = src[:pstart] + block + src[pend:]
    return src


def main() -> None:
    if not TARGET.is_file():
        raise RuntimeError('Run from the root of your amlogic Enigma2 checkout.')
    source = TARGET.read_text(encoding='utf-8')
    changed = transform(source)
    if changed == source:
        print('V2 already applied; no changes.')
        return
    # Verify both functions still exist in the transformed text before writing.
    locate_function(changed, 'pause')
    locate_function(changed, 'unpause')
    TARGET.write_text(changed, encoding='utf-8')
    print('Updated:', TARGET)
    print('Only eServiceMP3::pause diagnostics and local-video eServiceMP3::unpause behavior changed.')
    print('No driver, kernel, subtitle, remote mapping, or other files were touched.')
    print('Review: git diff --check && git diff -- lib/service/servicemp3.cpp')
    print('Compile and runtime testing on DreamTwo have NOT been performed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
