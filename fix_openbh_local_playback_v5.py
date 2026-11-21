#!/usr/bin/env python3
"""OpenBH DreamTwo local-video V5 patch (amlogic, after V4).

Features:
 * Fast pause enabled by default for local video. Emergency opt-out:
   touch /tmp/openbh_disable_fast_pause
 * Local-video UI position comes from zero-based GStreamer pipeline timeline,
   not the network-oriented startup decoder-time baseline correction.
 * A successful resume/explicit seek has a temporary UI position floor at the
   requested seek position until the asynchronous GStreamer seek catches up.
 * MediaPlayer OK toggles the film info overlay without bouncing it back open.

Limits: no changes to plugins other than built-in MediaPlayer, DVB, drivers,
         network streams, or keymaps. Run --check to preview; --apply to edit.
Requires current V3 MediaPlayer toggle and V4 fast pause code.
Not device-tested; run BitBake compile and validate on the actual DreamTwo.
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import re
import sys

CPP = Path('lib/service/servicemp3.cpp')
HDR = Path('lib/service/servicemp3.h')
PY = Path('lib/python/Plugins/Extensions/MediaPlayer/plugin.py')
MARK = 'OPENBH_LOCAL_PLAYBACK_V5'


def once(s: str, old: str, new: str, label: str) -> str:
    n = s.count(old)
    if n != 1:
        raise RuntimeError(f'{label}: expected one source anchor, found {n} (source may differ)')
    return s.replace(old, new, 1)


def function(s: str, name: str):
    expression = r'(?m)^RESULT\s+eServiceMP3::' + name + r'\s*\([^)]*\)\s*\{'
    matches = list(re.finditer(expression, s))
    if len(matches) != 1:
        raise RuntimeError(f'{name}: expected one function; found {len(matches)}')
    start = matches[0].start()
    brace = s.index('{', matches[0].start())
    depth = 0
    for j in range(brace, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                return start, j+1
    raise RuntimeError(f'{name}: unmatched braces')


def is_local_code(indent='\t'):
    return (
        indent + 'const bool openbh_local_movie = !m_sourceinfo.is_audio &&\n'
        + indent + '\t!m_ref.path.empty() &&\n'
        + indent + '\t(m_ref.path[0] == \'/\' || m_ref.path.compare(0, 7, "file://") == 0);'
    )


def patch_header(h: str):
    if MARK in h:
        if 'm_openbh_ui_seek_target_ns' not in h:
            raise RuntimeError('Header V5 marker exists, but expected member missing')
        return h
    needle = '\tgint64 m_openbh_fast_pause_target_ns = -1;'
    addition = (
        needle + '\n'
        '\t/* OPENBH_LOCAL_PLAYBACK_V5: UI can show the requested time while\n'
        '\t * an asynchronous local-file seek is still catching up. */\n'
        '\tgint64 m_openbh_ui_seek_target_ns = -1;\n'
        '\tgint64 m_openbh_ui_seek_deadline_us = 0;'
    )
    return once(h, needle, addition, 'header V4 member')


def patch_cpp(s: str):
    if MARK in s:
        if '/tmp/openbh_disable_fast_pause' not in s or 'm_openbh_ui_seek_target_ns' not in s:
            raise RuntimeError('C++ V5 marker exists but expected changes missing')
        return s
    if 'OPENBH_LOCAL_FAST_PAUSE_V4' not in s:
        raise RuntimeError('Apply V4 accurate fast-pause fix first')
    # Keep the V4 FLUSH|ACCURATE mechanism; only flip the default.
    s = once(s,
        'const bool fast_pause_test = g_file_test("/tmp/openbh_fast_pause_flush", G_FILE_TEST_EXISTS);',
        '/* OPENBH_LOCAL_PLAYBACK_V5: automatic for local video; opt out only on trouble. */\n'
        '\t\tconst bool fast_pause_test = !g_file_test("/tmp/openbh_disable_fast_pause", G_FILE_TEST_EXISTS);',
        'fast-pause opt-in flag')

    # Guard saved/resumed position against the decoder baseline being applied
    # at a newly sought-to nonzero time. Never affect network correction.
    a, b = function(s, 'seekToImpl')
    f = s[a:b]
    anchor = '\t/* A seek just happened: from here on the sink/pipeline itself reports\n'
    extra = '''\t/* OPENBH_LOCAL_PLAYBACK_V5: show the target immediately while the
\t * local-file seek completes asynchronously. The initial hardware sink
\t * decoder-time baseline must NOT be recomputed at the resume point. */
\tconst bool local_video_seek = !m_sourceinfo.is_audio && !m_ref.path.empty() &&
\t\t(m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0);
\tif (local_video_seek && m_currentTrickRatio == 1.0)
\t{
\t\tm_openbh_ui_seek_target_ns = m_last_seek_pos;
\t\tm_openbh_ui_seek_deadline_us = g_get_monotonic_time() + 5 * G_USEC_PER_SEC;
\t\tm_position_correction_enabled = false;
\t\tm_position_baseline_provisional = -1;
\t\teDebug("[OPENBH_LOCAL_PLAYBACK_V5] seek display target_ms=%lld",
\t\t\t(long long)(m_last_seek_pos / GST_MSECOND));
\t}
'''
    f = once(f, anchor, extra + anchor, 'seek position marker')
    s = s[:a] + f + s[b:]

    # Local files use GStreamer's native zero-based position directly. This
    # avoids the baseline confirmation window displaying 0 during startup,
    # and the same window re-basing a restored resume position to zero.
    a, b = function(s, 'getPlayPosition')
    f = s[a:b]
    anchor = '''\tpts_t raw;
\tRESULT res = getRawPlayPosition(raw);'''
    local_display = '''\t/* OPENBH_LOCAL_PLAYBACK_V5: local MP4/MKV use playbin time, which
\t * is the seek timeline. The sink's raw decoder time may be stale or
\t * nonzero-based during startup and after an early resume seek. */
\tconst bool local_video = !m_sourceinfo.is_audio && !m_ref.path.empty() &&
\t\t(m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0);
\tif (local_video && m_gst_playbin && m_state == stRunning)
\t{
\t\tgint64 pos_ns = -1;
\t\tconst bool queried = gst_element_query_position(m_gst_playbin, GST_FORMAT_TIME, &pos_ns) && pos_ns >= 0;
\t\tif (m_openbh_ui_seek_target_ns >= 0)
\t\t{
\t\t\tconst gint64 target = m_openbh_ui_seek_target_ns;
\t\t\tconst gint64 distance = queried ? (pos_ns >= target ? pos_ns - target : target - pos_ns) : G_MAXINT64;
\t\t\tif (queried && distance <= 2 * GST_SECOND)
\t\t\t{
\t\t\t\tm_openbh_ui_seek_target_ns = -1;
\t\t\t\tm_openbh_ui_seek_deadline_us = 0;
\t\t\t}
\t\t\telse if (g_get_monotonic_time() < m_openbh_ui_seek_deadline_us)
\t\t\t{
\t\t\t\tpts = (pts_t)(target / 11111LL);
\t\t\t\treturn 0;
\t\t\t}
\t\t\telse
\t\t\t{
\t\t\t\tm_openbh_ui_seek_target_ns = -1;
\t\t\t\tm_openbh_ui_seek_deadline_us = 0;
\t\t\t}
\t\t}
\t\tif (queried)
\t\t{
\t\t\tpts = (pts_t)(pos_ns / 11111LL);
\t\t\treturn 0;
\t\t}
\t}
\tpts_t raw;
\tRESULT res = getRawPlayPosition(raw);'''
    f = once(f, anchor, local_display, 'local display position')
    s = s[:a] + f + s[b:]
    return s


def patch_python(s: str):
    if MARK in s:
        if '_openbh_last_video_ok' not in s:
            raise RuntimeError('Python V5 marker exists but no OK guard')
        return s
    if 'OPENBH_MEDIAPLAYER_TOGGLE_V3' not in s:
        raise RuntimeError('V3 pause toggle missing; refusing to overwrite older MediaPlayer')
    s = once(s, 'from time import strftime\n', 'from time import strftime, monotonic\n', 'MediaPlayer time import')
    # The movie player screen is separate from the in-movie info overlay.
    # Debounce one OK action delivered twice by multiple action maps/key phases.
    anchor = '\tdef ok(self):\n\t\tif self.currList == "filelist":'
    toggle = '''\tdef ok(self):
\t\t# OPENBH_LOCAL_PLAYBACK_V5: when watching a local video, OK toggles
\t\t# only the movie infobar; do not re-open it from a duplicate action.
\t\tif (not self.shown and self.ext not in AUDIO_EXTENSIONS
\t\t\t\tand not self.isAudioCD and not self.playlist.isStopped()):
\t\t\tnow = monotonic()
\t\t\tif now - getattr(self, "_openbh_last_video_ok", -100.0) < 0.35:
\t\t\t\tprint("[OPENBH_LOCAL_PLAYBACK_V5] duplicate movie OK ignored")
\t\t\t\treturn
\t\t\tself._openbh_last_video_ok = now
\t\t\tself.hideMediaPlayerInfoBar.stop()
\t\t\tif self.mediaPlayerInfoBar.shown:
\t\t\t\tself.mediaPlayerInfoBar.hide()
\t\t\t\tprint("[OPENBH_LOCAL_PLAYBACK_V5] movie infobar HIDE")
\t\t\telse:
\t\t\t\tself.mediaPlayerInfoBar.show()
\t\t\t\tself.hideMediaPlayerInfoBar.start(5000, True)
\t\t\t\tprint("[OPENBH_LOCAL_PLAYBACK_V5] movie infobar SHOW")
\t\t\treturn
\t\tif self.currList == "filelist":'''
    s = once(s, anchor, toggle, 'MediaPlayer OK')
    return s


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Preview changes without writing')
    mode.add_argument('--apply', action='store_true', help='Apply changes after source validation')
    args = parser.parse_args()
    targets = [CPP, HDR, PY]
    missing = [str(p) for p in targets if not p.is_file()]
    if missing:
        raise RuntimeError('Run from the enigma2 repository root; missing ' + ', '.join(missing))
    originals = {p: p.read_text(encoding='utf-8') for p in targets}
    changed = {
        CPP: patch_cpp(originals[CPP]),
        HDR: patch_header(originals[HDR]),
        PY: patch_python(originals[PY]),
    }
    compile(changed[PY], str(PY), 'exec')
    if args.check:
        for p in targets:
            if changed[p] == originals[p]:
                print(f'{p}: V5 already applied')
            else:
                print(f'{p}: PATCH READY')
                sys.stdout.writelines(difflib.unified_diff(
                    originals[p].splitlines(True), changed[p].splitlines(True),
                    fromfile=str(p) + ' (before)', tofile=str(p) + ' (V5)', n=3))
        print('\nPREVIEW ONLY. No files modified.')
        return
    for p in targets:
        if originals[p] != changed[p]:
            p.write_text(changed[p], encoding='utf-8')
            print(f'Updated {p}')
        else:
            print(f'Unchanged {p} (V5 already applied)')
    print('Done. Review with git diff --check and git diff --stat; build-test before installing.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
