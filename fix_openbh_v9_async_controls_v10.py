#!/usr/bin/env python3
"""OpenBH DreamTwo local MoviePlayer V10 (EXPERIMENTAL).

Patch on top of V9 to move potential GStreamer blocking operations out of the
Enigma2 GUI thread. Track switches no longer disable/re-enable the text stream
synchronously. Keeps V9 subtitle clocks and V8 preroll/accurate seeks.

  --check      validate source and show unified diff (no changes)
  --apply      atomic edit, backup existing servicemp3.cpp
  --restore    restore the original V9 source from backup
  --diagnose   check expected source methods/markers

V10 is an uncompiled experiment. It cannot cure a truly deadlocked vendor sink;
it ensures GStreamer operations in these paths do not execute synchronously on
the Enigma2 main thread. See code comments for possible ordering limitations.
"""
from __future__ import annotations
import argparse
import difflib
import os
from pathlib import Path
import re
import shutil
import tempfile

CPP = Path('lib/service/servicemp3.cpp')
BACKUP = Path('lib/service/servicemp3.cpp.before_openatv_v10')
V9 = 'OPENBH_OPENATV_LOCAL_MOVIE_V9'
V10 = 'OPENBH_OPENATV_LOCAL_MOVIE_V10'
LOCAL = 'openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)'


def brace_end(s: str, op: int) -> int:
    """Skip comments/strings to find a source block's closing brace."""
    level, state, i = 0, 'code', op
    while i < len(s):
        c = s[i]
        n = s[i + 1] if i + 1 < len(s) else ''
        if state == 'line':
            if c == '\n': state = 'code'
        elif state == 'block':
            if c == '*' and n == '/': state = 'code'; i += 1
        elif state in ('str', 'char'):
            if c == '\\': i += 1
            elif (state == 'str' and c == '"') or (state == 'char' and c == "'"): state = 'code'
        elif c == '/' and n == '/': state = 'line'; i += 1
        elif c == '/' and n == '*': state = 'block'; i += 1
        elif c == '"': state = 'str'
        elif c == "'": state = 'char'
        elif c == '{': level += 1
        elif c == '}':
            level -= 1
            if level == 0: return i + 1
            if level < 0: raise RuntimeError('Unbalanced closing brace')
        i += 1
    raise RuntimeError('Unbalanced opening brace')


def method_region(src: str, name: str):
    candidates = list(re.finditer(r'(?m)^[ \t]*(?:RESULT|void)\s+eServiceMP3::'+re.escape(name)+(r'\s*\(\s*\)' if name in ('pause','unpause','stop') else r'\s*\('), src))
    if len(candidates) != 1: raise RuntimeError(f'{name}: expected one method, found {len(candidates)}')
    match = candidates[0]
    op = src.find('{', match.end())
    if op < 0 or op - match.end() > 600 or ';' in src[match.end():op]:
        raise RuntimeError(f'{name}: unexpected method signature')
    return match.start(), op, brace_end(src, op)


def modify_body(src: str, name: str, callback):
    _, op, end = method_region(src, name)
    orig = src[op+1:end-1]
    new = callback(orig)
    if new == orig: raise RuntimeError(f'{name}: no source modification')
    return src[:op+1] + new + src[end-1:]


HELPERS = '''
/* OPENBH_OPENATV_LOCAL_MOVIE_V10: no Enigma2 service object references in these
 * GStreamer workers. gst_element_call_async() retains its GstElement while
 * the request is queued; GDestroyNotify releases request memory. The serial
 * prevents a queued superseded request from being applied after a newer one.
 * Stop marks the old pipeline retired so queued operations are skipped.
 * A call that is already in gst_element_set_state() cannot be forcibly
 * cancelled; use log diagnostics if the hardware sink itself stays blocked.
 */
namespace {
struct OpenBHV10StateRequest {
    GstState state;
    guint serial;
};

struct OpenBHV10TextRequest {
    gint track;
    guint serial;
};

static void openbhV10AsyncState(GstElement *pipeline, gpointer opaque)
{
    OpenBHV10StateRequest *request = static_cast<OpenBHV10StateRequest *>(opaque);
    if (!pipeline) return;
    const bool latest = request->serial == GPOINTER_TO_UINT(
        g_object_get_data(G_OBJECT(pipeline), "openbh-v10-state-serial"));
    if (latest && !g_object_get_data(G_OBJECT(pipeline), "openbh-v10-retired")) {
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] worker state BEGIN target=%d serial=%u",
            (int)request->state, request->serial);
        const GstStateChangeReturn result = gst_element_set_state(pipeline, request->state);
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] worker state END target=%d result=%d",
            (int)request->state, (int)result);
    } else {
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] worker state SKIP stale/retired serial=%u",
            request->serial);
    }
    if (request->serial == GPOINTER_TO_UINT(
        g_object_get_data(G_OBJECT(pipeline), "openbh-v10-state-serial")))
        g_object_set_data(G_OBJECT(pipeline), "openbh-v10-state-pending", NULL);
}

static void openbhV10QueueState(GstElement *pipeline, GstState state)
{
    guint serial = GPOINTER_TO_UINT(
        g_object_get_data(G_OBJECT(pipeline), "openbh-v10-state-serial")) + 1;
    if (!serial) serial = 1;
    g_object_set_data(G_OBJECT(pipeline), "openbh-v10-state-serial", GUINT_TO_POINTER(serial));
    g_object_set_data(G_OBJECT(pipeline), "openbh-v10-state-pending", GINT_TO_POINTER(1));
    OpenBHV10StateRequest *request = g_new(OpenBHV10StateRequest, 1);
    request->state = state;
    request->serial = serial;
    gst_element_call_async(pipeline, openbhV10AsyncState, request, (GDestroyNotify)g_free);
}

static void openbhV10AsyncText(GstElement *pipeline, gpointer opaque)
{
    OpenBHV10TextRequest *request = static_cast<OpenBHV10TextRequest *>(opaque);
    if (!pipeline) return;
    const bool latest = request->serial == GPOINTER_TO_UINT(
        g_object_get_data(G_OBJECT(pipeline), "openbh-v10-text-serial"));
    if (latest && !g_object_get_data(G_OBJECT(pipeline), "openbh-v10-retired")) {
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] worker subtitle BEGIN track=%d serial=%u",
            request->track, request->serial);
        g_object_set(G_OBJECT(pipeline), "current-text", request->track, NULL);
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] worker subtitle END track=%d", request->track);
    } else {
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] worker subtitle SKIP stale/retired serial=%u",
            request->serial);
    }
    if (request->serial == GPOINTER_TO_UINT(
        g_object_get_data(G_OBJECT(pipeline), "openbh-v10-text-serial")))
        g_object_set_data(G_OBJECT(pipeline), "openbh-v10-text-pending", NULL);
}

static void openbhV10QueueText(GstElement *pipeline, gint track)
{
    guint serial = GPOINTER_TO_UINT(
        g_object_get_data(G_OBJECT(pipeline), "openbh-v10-text-serial")) + 1;
    if (!serial) serial = 1;
    g_object_set_data(G_OBJECT(pipeline), "openbh-v10-text-serial", GUINT_TO_POINTER(serial));
    g_object_set_data(G_OBJECT(pipeline), "openbh-v10-text-pending", GINT_TO_POINTER(1));
    OpenBHV10TextRequest *request = g_new(OpenBHV10TextRequest, 1);
    request->track = track;
    request->serial = serial;
    gst_element_call_async(pipeline, openbhV10AsyncText, request, (GDestroyNotify)g_free);
}

static bool openbhV10Busy(GstElement *pipeline)
{
    return pipeline &&
        (g_object_get_data(G_OBJECT(pipeline), "openbh-v10-state-pending") ||
         g_object_get_data(G_OBJECT(pipeline), "openbh-v10-text-pending"));
}
} // anonymous namespace, OpenBH V10
'''


def replace_local_branch(body, name: str, replacement: str):
    pat = re.compile(r'if\s*\(\s*openatv_v8_is_local_movie\s*\(\s*m_ref\s*,\s*m_is_live\s*,\s*m_sourceinfo\.is_audio\s*\)\s*\)\s*\{')
    found = list(pat.finditer(body))
    if len(found) != 1: raise RuntimeError(f'{name}: expected one local-file branch, found {len(found)}')
    m = found[0]
    end = brace_end(body, body.find('{', m.start(),m.end()))
    current = body[m.start():end]
    if name == 'pause':
        for term in ('PAUSE enter','GST_STATE_PAUSED', 'm_paused'):
            if term not in current: raise RuntimeError(f'{name}: V9 anchor missing {term}')
    elif name == 'unpause':
        for term in ('GST_STATE_PLAYING','m_paused'):
            if term not in current: raise RuntimeError(f'{name}: V8/V9 anchor missing {term}')
    return body[:m.start()] + replacement + body[end:]


def patch(source: str) -> str:
    if V10 in source:
        for term in ('worker state BEGIN', 'worker subtitle BEGIN', 'openbhV10QueueState', 'openbhV10Busy'):
            if term not in source: raise RuntimeError('Partial V10 found; use --restore')
        return source
    if V9 not in source or 'openatv_v8_is_local_movie' not in source:
        raise RuntimeError('This patch requires working V9 source (first apply V8, then V9)')
    _, pause_start, _ = method_region(source, 'pause')
    method_start, _, _ = method_region(source, 'pause')
    source = source[:method_start] + HELPERS + '\n' + source[method_start:]

    pause_repl = '''if (openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)) {
        if (!m_gst_playbin || m_state != stRunning) return -1;
        m_paused = true;
        m_currentTrickRatio = 1.0;
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] PAUSE queued (UI thread returns immediately)");
        openbhV10QueueState(m_gst_playbin, GST_STATE_PAUSED);
        return 0;
    }'''
    source = modify_body(source,'pause',lambda b:replace_local_branch(b,'pause',pause_repl))

    unpause_repl = '''if (openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)) {
        if (!m_gst_playbin || m_state != stRunning) return -1;
        m_paused = false;
        m_currentTrickRatio = 1.0;
        m_prev_decoder_time = -1;
        m_decoder_time_valid_state = 0;
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] RESUME queued (UI thread returns immediately)");
        openbhV10QueueState(m_gst_playbin, GST_STATE_PLAYING);
        return 0;
    }'''
    source = modify_body(source,'unpause',lambda b:replace_local_branch(b,'unpause',unpause_repl))

    def apply_text(b):
        if '"current-text", m_currentSubtitleStream' not in b:
            raise RuntimeError('applySubtitleStreamSwitch: expected original stream selection')
        head='''
    /* OPENBH_OPENATV_LOCAL_MOVIE_V10: avoid synchronous input-selector
     * reconfiguration and subsequent FLUSH seek from Enigma2's UI thread.
     * A new track may display only when its next cue arrives. */
    if (openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)) {
        if (!m_gst_playbin) return;
        m_subtitle_switch_needs_flush = false;
        eDebug("[OPENBH_OPENATV_LOCAL_MOVIE_V10] subtitle selected track=%d queued",
            m_currentSubtitleStream);
        openbhV10QueueText(m_gst_playbin, m_currentSubtitleStream);
        return;
    }
'''
        return head+b
    source = modify_body(source,'applySubtitleStreamSwitch',apply_text)

    def enable(b):
        expression='g_object_set (G_OBJECT (m_gst_playbin), "current-text", -1, NULL);'
        if b.count(expression)!=1: raise RuntimeError('enableSubtitles: expected one synchronous -1 reset')
        return b.replace(expression,'''/* V10: local movie goes directly to the newly selected text stream.
     * Avoid a synchronous -1 selector transition before the async switch. */
    if (!openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio))
        g_object_set(G_OBJECT(m_gst_playbin), "current-text", -1, NULL);''',1)
    source = modify_body(source,'enableSubtitles',enable)

    def disable(b):
        expression='g_object_set (G_OBJECT (m_gst_playbin), "current-text", m_currentSubtitleStream, NULL);'
        if b.count(expression)!=1: raise RuntimeError('disableSubtitles: expected one synchronous text switch')
        return b.replace(expression, '''if (openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio))
        openbhV10QueueText(m_gst_playbin, -1);
    else
        g_object_set(G_OBJECT(m_gst_playbin), "current-text", m_currentSubtitleStream, NULL);''',1)
    source = modify_body(source,'disableSubtitles',disable)

    def timer(b):
        anchor='''        pts_t now_pts = 0;'''
        if b.count(anchor)!=1 or 'subtitle clock unavailable, queue=' not in b:
            raise RuntimeError('pushSubtitles: V9 local scheduler structure not recognized')
        addition='''        if (m_paused || openbhV10Busy(m_gst_playbin)) {
            m_subtitle_sync_timer->start(150, true);
            return;
        }
'''
        return b.replace(anchor,addition+anchor,1)
    source=modify_body(source,'pushSubtitles',timer)

    def display_pos(b):
        expected='''        if (openatv_v8_position(m_gst_playbin, pts))
            return 0;'''
        if b.count(expected)!=1: raise RuntimeError('getPlayPosition: V8 direct clock anchor absent')
        replacement='''        /* During a native sink state/text transition, position queries can
         * wait on locks held by the streaming thread. Keep last good UI
         * value rather than blocking the Enigma2 GUI. */
        gint64 *v10_cached_pts = (gint64 *)g_object_get_data(
            G_OBJECT(m_gst_playbin), "openbh-v10-last-play-pts");
        if (openbhV10Busy(m_gst_playbin)) {
            if (v10_cached_pts) {
                pts = (pts_t)*v10_cached_pts;
                return 0;
            }
            return -1;
        }
        if (openatv_v8_position(m_gst_playbin, pts)) {
            if (!v10_cached_pts) {
                v10_cached_pts = g_new(gint64, 1);
                g_object_set_data_full(G_OBJECT(m_gst_playbin),
                    "openbh-v10-last-play-pts", v10_cached_pts, (GDestroyNotify)g_free);
            }
            *v10_cached_pts = (gint64)pts;
            return 0;
        }'''
        return b.replace(expected,replacement,1)
    source=modify_body(source,'getPlayPosition',display_pos)

    def stop(b):
        anchor='''        ++m_subtitle_generation;'''
        if b.count(anchor)!=1 or 'stop: clear local subtitle queues' not in b:
            raise RuntimeError('stop: expected V8 local stop block')
        return b.replace(anchor,'''        if (m_gst_playbin)
            g_object_set_data(G_OBJECT(m_gst_playbin), "openbh-v10-retired", GINT_TO_POINTER(1));
'''+anchor,1)
    source=modify_body(source,'stop',stop)

    for marker in ('worker state BEGIN','worker subtitle BEGIN','subtitle selected track=',
                   'openbh-v10-state-pending','PAUSE queued','RESUME queued'):
        if marker not in source: raise RuntimeError('Final validation missing '+marker)
    return source


def main():
    a = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    mode=a.add_mutually_exclusive_group(required=True)
    for key in ('check','apply','restore','diagnose'):
        mode.add_argument('--'+key,action='store_true')
    args=a.parse_args()
    if args.restore:
        if not BACKUP.exists(): raise RuntimeError('Missing V9 backup at '+str(BACKUP))
        shutil.copy2(BACKUP,CPP)
        print('Restored',CPP,'from',BACKUP)
        return
    if not CPP.is_file(): raise RuntimeError('Run from source root containing '+str(CPP))
    source=CPP.read_text(encoding='utf-8')
    if args.diagnose:
        print('V9 present:',V9 in source,'V10 present:',V10 in source)
        for name in ('pause','unpause','getPlayPosition','pushSubtitles',
                     'applySubtitleStreamSwitch','enableSubtitles','disableSubtitles','stop'):
            try:
                start,_,end=method_region(source,name)
                print(f'{name}: lines {source.count(chr(10),0,start)+1}-{source.count(chr(10),0,end)+1}')
            except RuntimeError as err: print(err)
        return
    modified=patch(source)
    if modified==source:
        print('V10 already present; no changes')
        return
    if args.check:
        print('V10 CHECK OK; no files changed')
        diff=''.join(difflib.unified_diff(source.splitlines(True),modified.splitlines(True),
             fromfile='servicemp3.cpp (V9)',tofile='servicemp3.cpp (V10)',n=3))
        print(diff[:36000])
        return
    if not BACKUP.exists(): shutil.copy2(CPP,BACKUP)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=CPP.parent,
                                     prefix='.openbh_v10_',delete=False) as h:
        h.write(modified);tmp=h.name
    try:
        os.chmod(tmp,CPP.stat().st_mode)
        os.replace(tmp,CPP)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print('Applied V10 to',CPP,'\nOriginal V9 saved at:',BACKUP)
    print('Next: git diff --check ; bitbake -f -c compile enigma2')

if __name__=='__main__':
    try: main()
    except (OSError,RuntimeError,ValueError) as exc:
        print('ERROR:',exc,file=__import__('sys').stderr)
        raise SystemExit(1)
