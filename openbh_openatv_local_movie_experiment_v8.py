#!/usr/bin/env python3
"""OpenBH -> OpenATV-like local MoviePlayer V8 experimental source transplant.

Uses the OpenATV-specific local video control strategy (preroll / deferred
resume seek / decoder independent media timeline / flush+accurate seeks /
subtitle timing domain / native GStreamer pause/resume). This is not a byte
for byte transplant: OpenATV's entire servicemp3.cpp is API-incompatible with
OpenBH's servicemp3.h and unrelated audio/stream/DVB-I classes.

This replaces the local-video behavior in EXISTING eServiceMP3 methods while
retaining the *ABI shape* and other service/factory implementations so the
user can attempt a meaningful OpenBH build. Existing local-playback V3..V6d
branches are bypassed for regular local files, not surgically uninstalled.

--check     verify source positions and display a diff WITHOUT writing
--apply     write source, save first-time pristine backup in checkout
--restore   restore pristine backup saved by --apply
--diagnose  show source compatibility markers

No hardware verification; experimental. No driver or DVB/PVR changes.
"""
from __future__ import annotations
import argparse
import difflib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

CPP = Path('lib/service/servicemp3.cpp')
BACKUP = Path('lib/service/servicemp3.cpp.before_openatv_v8')
MARK = 'OPENBH_OPENATV_LOCAL_MOVIE_V8'


def block_end(s, op):
    depth = 0
    state = 'code'
    i = op
    while i < len(s):
        c = s[i]
        n = s[i + 1] if i + 1 < len(s) else ''
        if state == 'line':
            if c == '\n': state = 'code'
        elif state == 'block':
            if c == '*' and n == '/': state='code'; i+=1
        elif state in ('quote','char'):
            if c == '\\': i+=1
            elif (state=='quote' and c=='"') or (state=='char' and c=="'"):
                state='code'
        elif c == '/' and n == '/': state='line'; i+=1
        elif c == '/' and n == '*': state='block'; i+=1
        elif c == '"': state='quote'
        elif c == "'": state='char'
        elif c == '{': depth+=1
        elif c == '}':
            depth-=1
            if depth==0: return i+1
            if depth<0: raise RuntimeError('Unbalanced braces')
        i+=1
    raise RuntimeError('Missing matching brace')


def fn_region(src, method):
    # Find signature from a line starting with a normal return type, then parse
    # until '{'. Protect from overmatching and class declaration prototypes.
    pattern = (r'(?m)^[ \t]*(?:RESULT|void)\s+eServiceMP3::' + re.escape(method) +
               (r'\s*\(\s*\)' if method == 'pause' else r'\s*\('))
    found=list(re.finditer(pattern,src))
    if len(found)!=1: raise RuntimeError('%s: expected one function definition, found %d' %(method,len(found)))
    m=found[0]
    op=src.find('{',m.end())
    if op==-1 or op-m.end()>600 or ';' in src[m.end():op]:
        raise RuntimeError('%s: unexpected signature (could not find opening brace)'%method)
    return m.start(), op, block_end(src,op)


def edit_function(src, name, modifier):
    start,op,end=fn_region(src,name)
    body=src[op+1:end-1]
    newbody=modifier(body)
    if newbody==body:
        raise RuntimeError('%s: no modification produced' % name)
    return src[:op+1]+newbody+src[end-1:]


def inject_head(body, content):
    return '\n'+content.rstrip()+'\n'+body


LOCAL = 'openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)'

HELPER = r'''/* OPENBH_OPENATV_LOCAL_MOVIE_V8: local MoviePlayer experiments.
 * Keep local media playback in GStreamer's time domain. For DVB/PVR and
 * network streams preserve OpenBH's original service behavior. */
static bool openatv_v8_is_local_movie(const eServiceReference &ref,
                                     bool is_live, bool is_audio)
{
    return !is_live && !is_audio && !ref.path.empty() &&
           (ref.path[0] == '/' || ref.path.compare(0, 7, "file://") == 0);
}
static bool openatv_v8_position(GstElement *playbin, pts_t &pts)
{
    gint64 ns = -1;
    if (!playbin || !gst_element_query_position(playbin, GST_FORMAT_TIME, &ns) || ns < 0)
        return false;
    pts = (pts_t)(ns / 11111LL);
    return true;
}
'''


def apply(src):
    if MARK in src:
        if 'openatv_v8_is_local_movie' in src and 'openatv-v8-start-stage' in src:
            return src
        raise RuntimeError('Partial V8 marker exists, use backup / --restore first')
    needed=['m_pending_start_position','tryApplyPendingStartOffset','m_subtitle_generation',
            'GST_MESSAGE_ASYNC_DONE','getRawPlayPosition', 'gst_element_query_position']
    missing=[x for x in needed if x not in src]
    if missing: raise RuntimeError('Incompatible OpenBH source missing: '+', '.join(missing))
    # Insert helper immediately before the unique service start member, after
    # constructor and source declarations; no reliance on V5/V6 patch markers.
    start,op,end=fn_region(src,'start')
    src=src[:start]+HELPER+'\n'+src[start:]

    # Media clock: OpenATV favors the decoded timeline and a GstPosition
    # fallback. The direct GstPosition timeline is chosen for local movies
    # because native Amlogic decoder timestamps can have a separate epoch.
    def position(b):
        top=f'''
    if ({LOCAL}) {{
        if (!m_gst_playbin || m_state != stRunning)
            return -1;
        if (openatv_v8_position(m_gst_playbin, pts))
            return 0;
        /* Keep last known position temporarily if the asynchronous seek is
         * still completing, instead of claiming playback is at zero. */
        if (m_last_seek_pos > 0) {{
            pts = (pts_t)(m_last_seek_pos / 11111LL);
            return 0;
        }}
        return -1;
    }}
'''
        return inject_head(b,top)
    src=edit_function(src,'getPlayPosition',position)

    # OpenATV-style startup: preroll regular local movies before requesting
    # PLAYING, then apply bookmark on the prerolled pipeline in ASYNC_DONE.
    def start_fn(b):
        p=r'\bret\s*=\s*gst_element_set_state\s*\(\s*m_gst_playbin\s*,\s*GST_STATE_PLAYING\s*\)\s*;'
        matches=list(re.finditer(p,b))
        if len(matches)!=1: raise RuntimeError('start: expected one initial PLAYING set_state, found %d'%len(matches))
        new=f'''/* {MARK}: preroll local file before first frame. */
        if ({LOCAL}) {{
            g_object_set_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage", GINT_TO_POINTER(1));
            ret = gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED);
            if (ret == GST_STATE_CHANGE_SUCCESS) {{
                /* No preroll message is guaranteed for a synchronous change. */
                g_object_set_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage", NULL);
                ret = gst_element_set_state(m_gst_playbin, GST_STATE_PLAYING);
            }}
            eDebug("[{MARK}] local startup preroll started");
        }} else {{
            ret = gst_element_set_state(m_gst_playbin, GST_STATE_PLAYING);
        }}'''
        return b[:matches[0].start()]+new+b[matches[0].end():]
    src=edit_function(src,'start',start_fn)

    def bus_fn(b):
        # Add handling at start of ASYNC_DONE, ahead of existing OpenBH
        # post-seek processing. Uses a GObject stage rather than new members.
        m=re.search(r'case\s+GST_MESSAGE_ASYNC_DONE\s*:\s*\{',b)
        if not m: raise RuntimeError('gstBusCall: missing ASYNC_DONE case')
        # The original case must first guard for playbin; insert AFTER it.
        tail=b[m.end():]
        gm=re.search(r'if\s*\(\s*GST_MESSAGE_SRC\s*\(\s*msg\s*\)\s*!=\s*GST_OBJECT\s*\(\s*m_gst_playbin\s*\)\s*\)\s*break\s*;',tail)
        if not gm or gm.start()>500: raise RuntimeError('gstBusCall: missing ASYNC_DONE playbin guard')
        pos=m.end()+gm.end()
        code=f'''
            /* {MARK}: OpenATV-inspired preroll and deferred bookmark. */
            if ({LOCAL}) {{
                int stage = GPOINTER_TO_INT(g_object_get_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage"));
                if (stage == 1) {{
                    const pts_t pending = m_pending_start_position;
                    if (pending > 0) {{
                        /* Native seek is nonblocking and stays in PAUSED until
                         * its own ASYNC_DONE, so no first-frame at 0. */
                        if (seekToImpl(pending, true) >= 0) {{
                            m_pending_start_position = -1;
                            if (m_start_events_deferred)
                                fireDeferredStartEvents();
                            g_object_set_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage", GINT_TO_POINTER(2));
                            eDebug("[{MARK}] initial bookmark seek pending=%lld", (long long)pending);
                            /* Continue normal ASYNC_DONE stream enumeration;
                             * stopping it here prevents subtitle discovery. */
                        }}
                        eDebug("[{MARK}] initial bookmark seek rejected, continue normally");
                    }}
                    g_object_set_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage", NULL);
                    gst_element_set_state(m_gst_playbin, GST_STATE_PLAYING);
                    eDebug("[{MARK}] preroll complete, PLAYING");
                }} else if (stage == 2) {{
                    g_object_set_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage", NULL);
                    m_event((iPlayableService*)this, evResumed);
                    gst_element_set_state(m_gst_playbin, GST_STATE_PLAYING);
                    eDebug("[{MARK}] initial bookmark prerolled, PLAYING");
                }}
            }}
'''
        return b[:pos]+code+b[pos:]
    src=edit_function(src,'gstBusCall',bus_fn)

    # V6d had an automatic startup rewind when the first position exceeded
    # three seconds; OpenATV does not do it and it corrupts subtitle timing.
    if 'OPENBH_LOCAL_PLAYBACK_V6D' in src:
        rewind = re.search(r'if\s*\(\s*position_ok\s*&&\s*startup_position_ns\s*>=\s*3\s*\*\s*GST_SECOND', src)
        if rewind:
            brace = src.find('{', rewind.end())
            if brace < 0 or brace - rewind.end() > 450:
                raise RuntimeError('V6d startup rewind structure unknown')
            end = block_end(src, brace)
            if 'seekToImpl(0, true)' not in src[brace:end]:
                raise RuntimeError('V6d unexpected rewind code; refusing to edit')
            src = src[:rewind.start()] + '/* V8: removed V6d forced startup rewind; OpenATV uses native playback timeline. */' + src[end:]

    # Do not use the old startup baseline or keyframe seeks for a local file;
    # local seek must stay in exact GStreamer timeline and discard old subs.
    def seek_fn(b):
        m=re.search(r'if\s*\(\s*!\s*gst_element_seek\s*\(',b)
        if not m: raise RuntimeError('seekToImpl: gst_element_seek not found')
        # Existing arguments may be either a cast expression (older OpenBH)
        # or a typed temporary such as openbh_v6_seek_flags (V6d). Preserve
        # the previous flags for all NONLOCAL paths to prevent regressions.
        hit=re.search(r'GST_FORMAT_TIME\s*,\s*(?P<flags>[\s\S]*?)\s*,\s*GST_SEEK_TYPE_SET',b)
        if not hit: raise RuntimeError('seekToImpl: flag argument not recognized')
        previous_flags=hit.group('flags').strip()
        if not (previous_flags.startswith('(GstSeekFlags)') or
                re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*',previous_flags)):
            raise RuntimeError('seekToImpl: unsupported seek flag expression '+repr(previous_flags[:120]))
        local=f'''\n    const bool openatv_v8_exact = {LOCAL};
    const GstSeekFlags openatv_v8_seek_flags = openatv_v8_exact ?
        (GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_ACCURATE) :
        {previous_flags};
'''
        b=b[:hit.start('flags')]+'openatv_v8_seek_flags'+b[hit.end('flags'):]
        # inject immediately before the seek call's surrounding if
        m=re.search(r'if\s*\(\s*!\s*gst_element_seek\s*\(',b)
        b=b[:m.start()].rstrip(' \t')+local+'\t'+b[m.start():]
        # After successful seek, reset embedded subtitle queue to new epoch.
        # Insert before the method's final return 0 (outside failure branch).
        last=list(re.finditer(r'(?m)^\s*return\s+0\s*;',b))
        if not last: raise RuntimeError('seekToImpl: success return missing')
        p=last[-1].start()
        clear=f'''
    if (openatv_v8_exact) {{
        ++m_subtitle_generation;
        m_subtitle_sync_timer->stop();
        m_dvb_subtitle_sync_timer->stop();
        m_subtitle_pages.clear();
        m_dvb_subtitle_pages.clear();
        m_prev_decoder_time = -1;
        m_decoder_time_valid_state = 0;
        eDebug("[{MARK}] accurate seek pts=%lld", (long long)to);
    }}
'''
        return b[:p]+clear+b[p:]
    src=edit_function(src,'seekToImpl',seek_fn)

    # In OpenATV the local pause sequence freezes the playbin and performs a
    # flush seek at the captured playback position; unpause is a state change.
    def pause_fn(b):
        return inject_head(b,f'''
    if ({LOCAL}) {{
        if (!m_gst_playbin || m_state != stRunning) return -1;
        gint64 ns = -1;
        const bool have_pos = gst_element_query_position(m_gst_playbin, GST_FORMAT_TIME, &ns) && ns >= 0;
        const GstStateChangeReturn ret = gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED);
        if (ret == GST_STATE_CHANGE_FAILURE) return -1;
        if (have_pos) {{
            gst_element_seek(m_gst_playbin, 1.0, GST_FORMAT_TIME,
                (GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_ACCURATE),
                GST_SEEK_TYPE_SET, ns, GST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);
            m_last_seek_pos = ns;
        }}
        m_paused = true;
        eDebug("[{MARK}] local PAUSE pos_ns=%lld", (long long)ns);
        return 0;
    }}
''')
    src=edit_function(src,'pause',pause_fn)

    def unpause_fn(b):
        return inject_head(b,f'''
    if ({LOCAL}) {{
        if (!m_gst_playbin || m_state != stRunning) return -1;
        if (gst_element_set_state(m_gst_playbin, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE)
            return -1;
        m_paused = false;
        m_currentTrickRatio = 1.0;
        m_prev_decoder_time = -1;
        m_decoder_time_valid_state = 0;
        eDebug("[{MARK}] local RESUME (no keyframe re-seek)");
        return 0;
    }}
''')
    src=edit_function(src,'unpause',unpause_fn)

    # Local embedded text/bitmap decode path modeled on the OpenATV buffer
    # callbacks, while using OpenBH's own subtitle stream types/widget.
    def pull_fn(b):
        return inject_head(b,f'''
    if ({LOCAL} && buffer && m_currentSubtitleStream >= 0 &&
        m_currentSubtitleStream < (int)m_subtitleStreams.size()) {{
        GstMapInfo map;
        if (!gst_buffer_map(buffer, &map, GST_MAP_READ)) return;
        const int kind = m_subtitleStreams[m_currentSubtitleStream].type;
        const GstClockTime start = GST_BUFFER_PTS(buffer);
        const GstClockTime dur = GST_BUFFER_DURATION(buffer);
        if (GST_CLOCK_TIME_IS_VALID(start)) {{
            if (kind == stDVB) {{
                m_dvb_subtitle_parser->processBuffer(map.data, map.size, start / GST_MSECOND);
            }} else if (kind < stVOB) {{
                const uint32_t start_ms = (uint32_t)(start / GST_MSECOND);
                const uint32_t duration_ms = GST_CLOCK_TIME_IS_VALID(dur) ?
                    (uint32_t)(dur / GST_MSECOND) : 2500U;
                std::string line((const char*)map.data, map.size);
                if (!line.empty() && line[line.length()-1] == '\\n')
                    line.erase(line.length()-1);
                const uint32_t end_ms = start_ms + duration_ms;
                m_subtitle_pages.insert(subtitle_pages_map_pair_t(end_ms,
                    subtitle_page_t(start_ms, end_ms, line)));
                m_subtitle_sync_timer->start(20, true);
            }}
            static unsigned v8_sub_debug = 0;
            if (v8_sub_debug++ < 20)
                eDebug("[{MARK}] embedded subtitle type=%d pts_ms=%lld size=%zu",
                    kind, (long long)(start / GST_MSECOND), (size_t)map.size);
        }} else {{
            eDebug("[{MARK}] invalid embedded subtitle PTS kind=%d", kind);
        }}
        gst_buffer_unmap(buffer, &map);
        return;
    }}
''')
    src=edit_function(src,'pullSubtitle',pull_fn)

    # For local movies, both subtitle scheduler and the displayed counter
    # must use the SAME GStreamer clock: never compare GstBuffer PTS against
    # an unrelated native /dev/tsync decoder epoch.
    def subtitle_clock(b, method):
        m=re.search(r'if\s*\(\s*getRawPlayPosition\s*\(\s*running_pts\s*\)\s*<\s*0\s*\)',b)
        if not m: raise RuntimeError(method+': raw subtitle clock anchor missing')
        replacement=f'''if (({LOCAL} &&
                openatv_v8_position(m_gst_playbin, running_pts)) ? false :
                getRawPlayPosition(running_pts) < 0)'''
        return b[:m.start()]+replacement+b[m.end():]
    src=edit_function(src,'pushSubtitles',lambda b:subtitle_clock(b,'pushSubtitles'))
    src=edit_function(src,'pushDVBSubtitles',lambda b:subtitle_clock(b,'pushDVBSubtitles'))

    # Clear or mark stale subtitle state when stopping, no need for global
    # drop_caches / invalidating RAM page cache.
    def stop_fn(b):
        return inject_head(b,f'''
    if ({LOCAL}) {{
        ++m_subtitle_generation;
        m_subtitle_sync_timer->stop();
        m_dvb_subtitle_sync_timer->stop();
        m_dvb_subtitle_pages.clear();
        m_subtitle_pages.clear();
        if (m_gst_playbin)
            g_object_set_data(G_OBJECT(m_gst_playbin), "openatv-v8-start-stage", NULL);
        eDebug("[{MARK}] stop: clear local subtitle queues");
    }}
''')
    src=edit_function(src,'stop',stop_fn)

    assert src.count('static bool openatv_v8_position')==1
    assert src.count('local startup preroll started')==1
    assert src.count('local RESUME (no keyframe re-seek)')==1
    return src


def diagnose(src):
    print('source bytes:',len(src),' v8:', MARK in src)
    for k in ('OPENBH_LOCAL_PLAYBACK_V5','OPENBH_LOCAL_PLAYBACK_V6D',
              'OPENBH_LOCAL_FAST_PAUSE_V4','m_pending_start_position',
              'tryApplyPendingStartOffset','GST_MESSAGE_ASYNC_DONE'):
        print(f'{k}: {src.count(k)}')
    for name in ('start','stop','pause','unpause','seekToImpl',
                 'getPlayPosition','pullSubtitle','pushSubtitles',
                 'pushDVBSubtitles','gstBusCall'):
        try:
            a,_,z=fn_region(src,name)
            print(name, 'OK at',src.count('\n',0,a)+1,'- ',src.count('\n',0,z)+1)
        except RuntimeError as exc:
            print(name,'FAIL:',exc)


def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    grp=ap.add_mutually_exclusive_group(required=True)
    for v in ('check','apply','restore','diagnose'):
        grp.add_argument('--'+v,action='store_true')
    args=ap.parse_args()
    if args.restore:
        if not BACKUP.is_file(): raise RuntimeError('Missing saved original: '+str(BACKUP))
        shutil.copy2(BACKUP,CPP)
        print('Restored:',CPP)
        return
    if not CPP.is_file(): raise RuntimeError('Run from your enigma2 checkout root; missing '+str(CPP))
    before=CPP.read_text(encoding='utf-8')
    if args.diagnose: diagnose(before); return
    after=apply(before)
    if before==after: print('Already applied, nothing changed');return
    if args.check:
        print('V8 CHECK OK: OpenATV-style experiment compatible with recognized source layout')
        delta=list(difflib.unified_diff(before.splitlines(True),after.splitlines(True),
            fromfile=str(CPP)+' (current)',tofile=str(CPP)+' (v8)',n=3))
        print(''.join(delta[:650]))
        if len(delta)>650: print('... (%d further diff lines omitted)'%(len(delta)-650))
        return
    if not BACKUP.exists():
        shutil.copy2(CPP,BACKUP)
        print('Saved original:',BACKUP)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=str(CPP.parent),prefix='.servicemp3_v8_',delete=False) as fh:
        fh.write(after)
        temp=fh.name
    try:
        os.chmod(temp,CPP.stat().st_mode)
        os.replace(temp,CPP)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    print('Applied OpenATV-style local MoviePlayer V8 to',CPP)
    print('Test: git diff --check ; bitbake -f -c compile enigma2')
    print('Restore: python3 openbh_openatv_local_movie_experiment_v8.py --restore')


if __name__=='__main__':
    try: main()
    except (OSError,RuntimeError,AssertionError,ValueError) as exc:
        print('ERROR:',exc,file=sys.stderr)
        sys.exit(1)
