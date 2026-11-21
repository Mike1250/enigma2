#!/usr/bin/env python3
"""OpenBH DreamTwo V6d: accurate local-file seeks, guarded startup check,
and best-effort glibc heap trim after GStreamer stop.

Applies to V5 source. Unlike V6/V6b/V6c, it searches the StopWorkerArgs
structure and worker by their names, not by adjacent comments/whitespace.

--check    validate everything and print an exact diff, without editing files
--apply    validate everything first, then write a single C++ file
--diagnose print localized anchor context to help inspect source divergence

No edits to drivers, kernel, DVB, MediaPlayer Python or other files.
A successful source transformation is NOT proof that a build or hardware
playback will work: compile and test on DreamTwo before relying on it.
"""
from __future__ import annotations
import argparse
import difflib
from pathlib import Path
import re
import sys

CPP = Path('lib/service/servicemp3.cpp')
MARK = 'OPENBH_LOCAL_PLAYBACK_V6D'


def single_match(pattern: str, source: str, description: str, flags=0):
    found = list(re.finditer(pattern, source, flags))
    if len(found) != 1:
        raise RuntimeError(f'{description}: expected one match, got {len(found)}; no files changed')
    return found[0]


def block_end(source: str, brace: int):
    """Find matching C++ closing brace, ignoring comments and quoted strings."""
    depth, i, mode = 0, brace, 'code'
    while i < len(source):
        c = source[i]
        n = source[i+1] if i+1 < len(source) else ''
        if mode == 'line':
            if c == '\n': mode = 'code'
        elif mode == 'block':
            if c == '*' and n == '/': mode, i = 'code', i+1
        elif mode in ('string', 'char'):
            if c == '\\': i += 1
            elif (mode == 'string' and c == '"') or (mode == 'char' and c == "'"):
                mode = 'code'
        elif c == '/' and n == '/':
            mode, i = 'line', i+1
        elif c == '/' and n == '*':
            mode, i = 'block', i+1
        elif c == '"': mode = 'string'
        elif c == "'": mode = 'char'
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i + 1
            if depth < 0: break
        i += 1
    raise RuntimeError('Unbalanced C++ braces near source position %d' % brace)


def named_block(source, pattern, label):
    m = single_match(pattern, source, label, re.M)
    op = source.find('{', m.start(), m.end()+10)
    if op < 0: raise RuntimeError(label + ': missing opening brace')
    return m.start(), block_end(source, op)


def change_once(src, pattern, fn, label, flags=0):
    m = single_match(pattern, src, label, flags)
    return src[:m.start()] + fn(m) + src[m.end():]


def change_function(src, name_pattern, label, transform):
    a, b = named_block(src, name_pattern, label)
    changed = transform(src[a:b])
    return src[:a] + changed + src[b:]


def patch(src):
    if MARK in src:
        for marker in ('local_exact_seek', 'wasLocalMovie', 'malloc_trim(0)', 'startup_position_ns'):
            if marker not in src:
                raise RuntimeError('V6d marker exists but feature is incomplete: ' + marker)
        return src
    if 'OPENBH_LOCAL_PLAYBACK_V6' in src:
        raise RuntimeError('Another V6 variant is present; refusing to stack patches')
    if 'OPENBH_LOCAL_PLAYBACK_V5' not in src or 'OPENBH_LOCAL_FAST_PAUSE_V4' not in src:
        raise RuntimeError('This patch requires V5 + V4 in servicemp3.cpp')

    if '#include <malloc.h>' not in src:
        src = change_once(src, r'(?m)^([ \t]*)#include\s*<cstdlib>\s*$',
            lambda m: m.group(0) + '\n#ifdef __GLIBC__\n#include <malloc.h>\n#endif',
            'malloc include')

    def patch_seek(f):
        # Just the seek flags, not surrounding source formatting or comments.
        flags = re.compile(
            r'\(\s*GstSeekFlags\s*\)\s*\(\s*GST_SEEK_FLAG_FLUSH\s*\|\s*'
            r'\(\s*accurate\s*\?\s*GST_SEEK_FLAG_ACCURATE\s*:\s*GST_SEEK_FLAG_KEY_UNIT\s*\)\s*\)'
        )
        flag_match = single_match(flags.pattern, f, 'seekToImpl seek flags')
        declaration = '''/* OPENBH_LOCAL_PLAYBACK_V6D: seek saved positions precisely on local
\t * videos. Streaming and trick-play retain their existing key-unit policy. */
\tconst bool local_exact_seek = !m_sourceinfo.is_audio && !m_is_live &&
\t\t!m_ref.path.empty() &&
\t\t(m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0) &&
\t\tm_currentTrickRatio == 1.0;
\tconst GstSeekFlags openbh_v6_seek_flags = (GstSeekFlags)(GST_SEEK_FLAG_FLUSH |
\t\t((accurate || local_exact_seek) ? GST_SEEK_FLAG_ACCURATE : GST_SEEK_FLAG_KEY_UNIT));
\t'''
        # Insert declaration immediately before the `if (!gst_element_seek(`
        # corresponding to the flags. No assumptions about prior statements.
        before = f[:flag_match.start()]
        calls = list(re.finditer(r'\bif\s*\(\s*!\s*gst_element_seek\s*\(', before))
        if len(calls) != 1:
            raise RuntimeError(f'seekToImpl gst_element_seek call: expected 1, got {len(calls)}')
        call = calls[0]
        f = f[:call.start()] + declaration + f[call.start():]
        f = change_once(f, flags.pattern, lambda _: 'openbh_v6_seek_flags', 'local seek flags')
        if 'OPENBH_LOCAL_PLAYBACK_V5: show the target' not in f:
            raise RuntimeError('seekToImpl: missing V5 position display code')
        return f
    src = change_function(src,
        r'^[ \t]*RESULT\s+eServiceMP3::seekToImpl\s*\(\s*pts_t\s+to\s*,\s*bool\s+accurate\s*\)\s*\{',
        'seekToImpl', patch_seek)

    # First PLAYING transition: debug and only rewind an unresumed local
    # video when GStreamer already reports an anomalous >=3 s offset.
    start_pattern = r'(?m)^(?P<indent>[ \t]+)tryApplyPendingStartOffset\s*\(\s*\)\s*;[ \t]*$'
    call = single_match(start_pattern, src, 'startup call', re.M)
    if not re.search(r'\bif\s*\(\s*!m_initial_start\s*\)', src[call.end():call.end()+4500]):
        raise RuntimeError('startup call present, but nearby first-start guard missing')
    indent = call.group('indent')
    if not indent: raise RuntimeError('startup block has no indentation')
    snippet = '''/* OPENBH_LOCAL_PLAYBACK_V6D: initial local playback position diagnostic.
 * Never reset a saved seek position. */
if (!m_initial_start && !m_is_live && !m_sourceinfo.is_audio &&
    !m_ref.path.empty() &&
    (m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0))
{
    gint64 startup_position_ns = -1;
    const bool position_ok = gst_element_query_position(m_gst_playbin,
        GST_FORMAT_TIME, &startup_position_ns) && startup_position_ns >= 0;
    eDebug("[OPENBH_LOCAL_PLAYBACK_V6D] startup first_playing_ms=%lld saved_seek_ms=%lld",
        (long long)(position_ok ? startup_position_ns / GST_MSECOND : -1),
        (long long)(m_last_seek_pos / GST_MSECOND));
    if (position_ok && startup_position_ns >= 3 * GST_SECOND &&
        m_last_seek_pos == 0 && m_openbh_ui_seek_target_ns < 0 &&
        m_pending_start_position < 0)
    {
        const RESULT reset_result = seekToImpl(0, true);
        eDebug("[OPENBH_LOCAL_PLAYBACK_V6D] startup rewind-to-zero result=%d",
            (int)reset_result);
    }
}'''
    injected = '\n' + '\n'.join(indent + line for line in snippet.splitlines())
    src = src[:call.end()] + injected + src[call.end():]

    def patch_struct(block):
        if 'wasLocalMovie' in block: raise RuntimeError('StopWorkerArgs already has wasLocalMovie field')
        # Named structure bounds isolate the field, even when comments or
        # additional fields follow it.
        return change_once(block, r'(?m)^(?P<i>[ \t]*)bool\s+hadVideo\s*;',
            lambda m: m.group(0) + '\n' + m.group('i') + 'bool wasLocalMovie;',
            'StopWorkerArgs.hadVideo')
    src = change_function(src, r'^[ \t]*struct\s+StopWorkerArgs\s*\{',
                          'StopWorkerArgs', patch_struct)

    def patch_worker(block):
        block = change_once(block,
            r'(?m)^(?P<i>[ \t]*)(?:const\s+)?bool\s+hadVideo\s*=\s*args\s*->\s*hadVideo\s*;',
            lambda m: m.group(0) + '\n' + m.group('i') + 'const bool wasLocalMovie = args->wasLocalMovie;',
            'stopWorker flag')
        comment = '''\n#ifdef __GLIBC__
    /* OPENBH_LOCAL_PLAYBACK_V6D: return free malloc arenas, not page cache. */
    if (wasLocalMovie)
    {
        const int trimmed = malloc_trim(0);
        eDebug("[OPENBH_LOCAL_PLAYBACK_V6D] local stop malloc_trim=%d", trimmed);
    }
#else
    (void)wasLocalMovie;
#endif'''
        block = change_once(block, r'\bgst_object_unref\s*\(\s*playbin\s*\)\s*;',
            lambda m: m.group(0) + comment, 'stopWorker gst_object_unref')
        return block
    src = change_function(src,
        r'^[ \t]*gpointer\s+stopWorker\s*\(\s*gpointer\s+data\s*\)\s*\{',
        'stopWorker', patch_worker)

    # Append a 4th field to the actual initializer, without assuming a
    # single line, the exact PiP comment, or whitespace style.
    init = single_match(r'\bnew\s+StopWorkerArgs\s*\{', src, 'StopWorkerArgs construction')
    init_brace = src.find('{', init.start(), init.end())
    end = block_end(src, init_brace)
    previous = src[init_brace+1:end-1]
    if 'm_gst_playbin' not in previous or 'watchdog' not in previous or 'videoSink' not in previous:
        raise RuntimeError('StopWorkerArgs initializer has unexpected fields; refusing to guess')
    local = '''!m_sourceinfo.is_audio && !m_ref.path.empty() &&
        (m_ref.path[0] == '/' || m_ref.path.compare(0, 7, "file://") == 0)'''
    src = src[:end-1] + ',\n        ' + local + src[end-1:]

    for expected in ('local_exact_seek', 'startup_position_ns', 'wasLocalMovie', 'malloc_trim(0)', MARK):
        if expected not in src:
            raise RuntimeError('Final validation failed: ' + expected)
    return src


def diagnose(src):
    print('SOURCE:', CPP, '| bytes:', len(src))
    checks = [
        ('V5', r'OPENBH_LOCAL_PLAYBACK_V5'),
        ('V4', r'OPENBH_LOCAL_FAST_PAUSE_V4'),
        ('V6', r'OPENBH_LOCAL_PLAYBACK_V6[D]?(?:[^A-Za-z0-9_]|$)'),
        ('stop struct', r'(?m)^\s*struct\s+StopWorkerArgs\b'),
        ('hadVideo field', r'\bbool\s+hadVideo\s*;'),
        ('stop worker', r'(?m)^\s*gpointer\s+stopWorker\b'),
        ('worker initializer', r'new\s+StopWorkerArgs\s*\{'),
        ('startup call', r'tryApplyPendingStartOffset\(\);'),
        ('seek flags', r'GST_SEEK_FLAG_FLUSH\s*\|\s*\(\s*accurate'),
    ]
    lines = src.splitlines()
    for label, pattern in checks:
        hits = [i + 1 for i, line in enumerate(lines) if re.search(pattern, line)]
        print(f'{label}: count={len(hits)} lines={hits[:8]}')
    m = re.search(r'\bstruct\s+StopWorkerArgs\s*\{', src)
    if m:
        ln = src.count('\n', 0, m.start())
        print('\n===== StopWorkerArgs context =====')
        print('\n'.join(f'{i+1:5}: {lines[i]}' for i in range(max(0,ln-3), min(len(lines),ln+15))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--diagnose', action='store_true')
    args = ap.parse_args()
    if not CPP.is_file():
        raise RuntimeError('Run from the root of your local Enigma2 Git checkout')
    old = CPP.read_text(encoding='utf-8')
    if args.diagnose:
        diagnose(old)
        return
    new = patch(old)
    if old == new:
        print('V6d already applied; source unchanged')
    elif args.check:
        sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
            fromfile=str(CPP) + ' (before)', tofile=str(CPP) + ' (V6d)', n=3))
        print('\nPREVIEW ONLY: all anchors validated; no files modified.')
    else:
        CPP.write_text(new, encoding='utf-8')
        print('Patched', CPP, 'with V6d. Run: git diff --check')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as e:
        print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
