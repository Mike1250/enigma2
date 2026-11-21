#!/usr/bin/env python3
"""DreamTwo/OpenBH local-video V6c, applies on top of V5 (amlogic branch).

- Accurate GStreamer seeking for *ordinary local-video* position restores,
  instead of KEY_UNIT (which may land at an earlier GOP/keyframe).
- A guarded one-shot restart at zero if playbin has already progressed >= 3 s
  at the FIRST PAUSED->PLAYING transition of an unresumed local file.
  If playback first reaches five seconds much later, log evidence rather
  than falsifying reported time or overwriting user seek/resume positions.
- Once GStreamer's asynchronous stop worker has finished tearing down a
  local-video pipeline, request glibc malloc_trim(0) to return currently free
  malloc pages to the OS. This does NOT clear normal Linux page cache or fix
  retained allocations / native decoder buffers.

No changes to DVB playback, drivers, Python plug-ins, or V5 key handling.
--check previews; --apply changes servicemp3.cpp only.
Requires V5. Not yet cross-compiled or hardware-tested.
"""
from __future__ import annotations
import argparse
import difflib
from pathlib import Path
import re
import sys

CPP = Path('lib/service/servicemp3.cpp')
MARK = 'OPENBH_LOCAL_PLAYBACK_V6'


def replace_once(src: str, old: str, new: str, description: str) -> str:
    n = src.count(old)
    if n != 1:
        raise RuntimeError(f'{description}: expected one anchor; found {n}; no files changed')
    return src.replace(old, new, 1)


def fn_region(src: str, name: str, args: str):
    regex = rf'(?m)^(?:RESULT|gpointer)\s+(?:eServiceMP3::)?{re.escape(name)}\s*{args}\s*\{{'
    matches = list(re.finditer(regex, src))
    if len(matches) != 1:
        raise RuntimeError(f'{name}: expected one function, found {len(matches)}')
    start = matches[0].start()
    brace = src.index('{', start)
    depth = 0
    # Braces inside comments in this version are not balanced-sensitive in the
    # selected short functions; fail closed if final function signature diverges.
    for i in range(brace, len(src)):
        if src[i] == '{': depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0: return start, i+1
    raise RuntimeError(f'{name}: unterminated function')


def patch(src: str) -> str:
    if MARK in src:
        must_have = ('malloc_trim(0)', 'local_exact_seek', 'startup_position_ns')
        if not all(x in src for x in must_have):
            raise RuntimeError('V6 marker exists, but patch looks incomplete')
        return src
    if 'OPENBH_LOCAL_PLAYBACK_V5' not in src or 'OPENBH_LOCAL_FAST_PAUSE_V4' not in src:
        raise RuntimeError('This patch requires your V5 + V4 servicemp3.cpp source')

    # Headers for the glibc-only best-effort heap trim after a movie stops.
    if '#include <malloc.h>' not in src:
        src = replace_once(src, '#include <cstdlib>\n',
            '#include <cstdlib>\n#ifdef __GLIBC__\n#include <malloc.h>\n#endif\n',
            'glibc malloc.h include')

    # FIX: user-initiated saved-position seeks were still KEY_UNIT by default.
    a, b = fn_region(src, 'seekToImpl', r'\(\s*pts_t\s+to\s*,\s*bool\s+accurate\s*\)')
    section = src[a:b]
    old = '''\tm_last_seek_pos = to * 11111LL;
\tif (!gst_element_seek (m_gst_playbin, m_currentTrickRatio, GST_FORMAT_TIME,
\t\t(GstSeekFlags)(GST_SEEK_FLAG_FLUSH | (accurate ? GST_SEEK_FLAG_ACCURATE : GST_SEEK_FLAG_KEY_UNIT)),'''
    new = '''\tm_last_seek_pos = to * 11111LL;
\t/* OPENBH_LOCAL_PLAYBACK_V6: the normal local-file seek path (including
\t * saved-position restores) must not snap to a previous keyframe. Keep
\t * the existing KEY_UNIT strategy for streaming and trick playback. */
\tconst bool local_exact_seek = !m_sourceinfo.is_audio && !m_is_live &&
\t\t!m_ref.path.empty() &&
\t\t(m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0) &&
\t\tm_currentTrickRatio == 1.0;
\tconst GstSeekFlags openbh_v6_seek_flags = (GstSeekFlags)(GST_SEEK_FLAG_FLUSH |
\t\t((accurate || local_exact_seek) ? GST_SEEK_FLAG_ACCURATE : GST_SEEK_FLAG_KEY_UNIT));
\tif (!gst_element_seek (m_gst_playbin, m_currentTrickRatio, GST_FORMAT_TIME,
\t\topenbh_v6_seek_flags,'''
    section = replace_once(section, old, new, 'seekToImpl flags')
    section = replace_once(section,
        '''\t/* OPENBH_LOCAL_PLAYBACK_V5: show the target immediately while the''',
        '''\tif (local_exact_seek)
\t\teDebug("[OPENBH_LOCAL_PLAYBACK_V6] accurate local seek target_ms=%lld",
\t\t\t(long long)(m_last_seek_pos / GST_MSECOND));
\t/* OPENBH_LOCAL_PLAYBACK_V5: show the target immediately while the''',
        'seek trace')
    src = src[:a] + section + src[b:]

    # The V6 and V6b scripts incorrectly require the following first-start
    # guard to be *immediately adjacent* with identical indentation. Match the
    # one executable function call on its own line instead. Preserve all
    # intervening comments, blank lines, and user-specific formatting.
    startup_call = re.compile(
        r'(?m)^(?P<indent>[ \t]+)tryApplyPendingStartOffset\(\);[ \t]*(?=\r?$)'
    )
    calls = list(startup_call.finditer(src))
    if len(calls) != 1:
        found = re.findall(r'(?m)^.*tryApplyPendingStartOffset\(\);.*$', src)
        raise RuntimeError(
            f'startup first PLAYING: expected one standalone call; found '
            f'{len(calls)}; matching lines: {found[:6]!r}; no files changed'
        )
    call = calls[0]
    # Confirm we found the startup event handler, not an unrelated call.
    # This deliberately tolerates other statements or comments in between.
    tail = src[call.end():call.end() + 3500]
    if not re.search(r'\bif[ \t]*\([ \t]*![ \t]*m_initial_start[ \t]*\)', tail):
        raise RuntimeError(
            'startup first PLAYING: found the call but no following '
            'first-start guard within 3500 bytes; no files changed'
        )
    indent = call.group('indent')
    snippet = '''/* OPENBH_LOCAL_PLAYBACK_V6: inspect the first position when
 * local playback reaches PLAYING. Do not reset an explicit resume point. */
if (!m_initial_start && !m_is_live && !m_sourceinfo.is_audio &&
    !m_ref.path.empty() &&
    (m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0))
{
    gint64 startup_position_ns = -1;
    const bool initial_position_valid = gst_element_query_position(
        m_gst_playbin, GST_FORMAT_TIME, &startup_position_ns) && startup_position_ns >= 0;
    eDebug("[OPENBH_LOCAL_PLAYBACK_V6] startup first_playing_ms=%lld saved_seek_ms=%lld",
        (long long)(initial_position_valid ? startup_position_ns / GST_MSECOND : -1),
        (long long)(m_last_seek_pos / GST_MSECOND));
    if (initial_position_valid && startup_position_ns >= 3 * GST_SECOND &&
        m_last_seek_pos == 0 && m_openbh_ui_seek_target_ns < 0)
    {
        const RESULT reset_result = seekToImpl(0, true);
        eDebug("[OPENBH_LOCAL_PLAYBACK_V6] startup rewind-to-zero result=%d",
            (int)reset_result);
    }
}'''
    inserted = '\n'.join(indent + line for line in snippet.splitlines())
    src = src[:call.end()] + '\n' + inserted + src[call.end():]

    # Stop worker is ALREADY the correct place for GStreamer deallocation.
    # This request just returns free glibc arenas after teardown; intentionally
    # never write /proc/sys/vm/drop_caches or kill unrelated system caches.
    src = replace_once(src,
        '''\tbool hadVideo;
};
/* gst_element_set_state(..., GST_STATE_NULL)''',
        '''\tbool hadVideo;
\tbool wasLocalMovie;
};
/* gst_element_set_state(..., GST_STATE_NULL)''',
        'StopWorkerArgs')
    a, b = fn_region(src, 'stopWorker', r'\(\s*gpointer\s+data\s*\)')
    section = src[a:b]
    section = replace_once(section, '''\tbool hadVideo = args->hadVideo;
\tdelete args;''',
        '''\tbool hadVideo = args->hadVideo;
\tbool wasLocalMovie = args->wasLocalMovie;
\tdelete args;''', 'worker local flag')
    section = replace_once(section, '''\tgst_object_unref(playbin);

\tint remaining''',
        '''\tgst_object_unref(playbin);
\t/* OPENBH_LOCAL_PLAYBACK_V6: reclaim *free userspace malloc pages* once
\t * GStreamer has finished tearing down the local movie. File cache and
\t * allocations owned elsewhere remain managed by the kernel. */
#ifdef __GLIBC__
\tif (wasLocalMovie)
\t{
\t\tconst int trimmed = malloc_trim(0);
\t\teDebug("[OPENBH_LOCAL_PLAYBACK_V6] local movie stop: malloc_trim=%d", trimmed);
\t}
#else
\t(void)wasLocalMovie;
#endif

\tint remaining''', 'trim on worker release')
    src = src[:a] + section + src[b:]
    call = '''new StopWorkerArgs{m_gst_playbin, watchdog, videoSink != NULL && m_pip_decoder < 0 /* a PiP must never blank the primary decoder */}'''
    replacement = '''new StopWorkerArgs{m_gst_playbin, watchdog,
\t\tvideoSink != NULL && m_pip_decoder < 0 /* a PiP must never blank the primary decoder */,
\t\t!m_sourceinfo.is_audio && !m_ref.path.empty() &&
\t\t(m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0)}'''
    src = replace_once(src, call, replacement, 'worker creation')
    return src


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check', action='store_true', help='Preview changes')
    g.add_argument('--apply', action='store_true', help='Apply after all anchors validate')
    args = ap.parse_args()
    if not CPP.is_file():
        raise RuntimeError('Run from the root of your amlogic Enigma2 Git checkout')
    old = CPP.read_text(encoding='utf-8')
    new = patch(old)
    if old == new:
        print('V6 already applied (no changes).')
    elif args.check:
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True),
            fromfile='lib/service/servicemp3.cpp (V5)',
            tofile='lib/service/servicemp3.cpp (V6)', n=3))
        print('PREVIEW ONLY. No files modified.')
    else:
        CPP.write_text(new, encoding='utf-8')
        print('Updated lib/service/servicemp3.cpp')
        print('Run: git diff --check && git diff --stat')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as e:
        print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
