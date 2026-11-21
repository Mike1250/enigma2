#!/usr/bin/env python3
"""OpenBH/DreamTwo V9 experiment: unblock V8 pause + repair/instrument embedded text subtitles.

Apply ON TOP OF the V8 OpenATV-style local MoviePlayer experiment.
Does not touch kernel, DVB, external service providers, or MediaPlayer plugin.py.

  --check       confirm recognized V8 source, show diff, do not modify
  --apply       make atomic edit after all checks; preserve original *.before_openatv_v9
  --restore     restore the V8 source saved on first --apply
  --diagnose    show presence of expected methods and markers

V9 intentionally prioritizes stable pause over fast pause. The direct
flushing seek after PAUSED, which may block Enigma2's UI thread, is removed.
"""
from __future__ import annotations
import argparse
import difflib
import os
from pathlib import Path
import re
import shutil
import tempfile

CPP=Path('lib/service/servicemp3.cpp')
BACKUP=Path('lib/service/servicemp3.cpp.before_openatv_v9')
V8='OPENBH_OPENATV_LOCAL_MOVIE_V8'
V9='OPENBH_OPENATV_LOCAL_MOVIE_V9'


def brace_end(s,op):
    level=0; state='code'; i=op
    while i<len(s):
        c=s[i]; n=s[i+1] if i+1<len(s) else ''
        if state=='line':
            if c=='\n':state='code'
        elif state=='block':
            if c=='*' and n=='/':state='code';i+=1
        elif state in ('str','chr'):
            if c=='\\':i+=1
            elif (state=='str' and c=='"') or (state=='chr' and c=="'"):state='code'
        elif c=='/' and n=='/':state='line';i+=1
        elif c=='/' and n=='*':state='block';i+=1
        elif c=='"':state='str'
        elif c=="'":state='chr'
        elif c=='{':level+=1
        elif c=='}':
            level-=1
            if level==0:return i+1
            if level<0:raise RuntimeError('C++ unbalanced braces')
        i+=1
    raise RuntimeError('C++ missing closing brace')


def region(s,method):
    rx=re.compile(r'(?m)^[ \t]*(?:RESULT|void)\s+eServiceMP3::'+re.escape(method)+(r'\s*\(\s*\)' if method=='pause' else r'\s*\('))
    found=list(rx.finditer(s))
    if len(found)!=1:raise RuntimeError(f'{method}: expected one definition, found {len(found)}')
    m=found[0]; op=s.find('{',m.end())
    if op<0 or op-m.end()>600 or ';' in s[m.end():op]:
        raise RuntimeError(f'{method}: unexpected signature')
    return op,brace_end(s,op)


def replace_body(s,method,cb):
    op,end=region(s,method)
    old=s[op+1:end-1]
    new=cb(old)
    if new==old:raise RuntimeError(f'{method}: replacement made no change')
    return s[:op+1]+new+s[end-1:]


def replace_v8_pause(b):
    # Restrict to the V8 injected local branch (first block of pause()).
    match=re.search(r'if\s*\(\s*openatv_v8_is_local_movie\s*\(\s*m_ref\s*,\s*m_is_live\s*,\s*m_sourceinfo\.is_audio\s*\)\s*\)\s*\{',b)
    if not match:raise RuntimeError('pause: V8 local branch missing')
    openbrace=b.find('{',match.start(),match.end())
    close=brace_end(b,openbrace)
    original=b[match.start():close]
    for anchor in ('gst_element_query_position','GST_STATE_PAUSED','gst_element_seek','local PAUSE pos_ns'):
        if anchor not in original:raise RuntimeError(f'pause: V8 source mismatch, missing {anchor}')
    safe=f'''/* {V9}: no blocking position-query/FLUSH seek on remote-key path.
     * The V8 seek within pause could deadlock or stall GStreamer on DreamTwo.
     * Keep PAUSED state change, and log its return value. */
    if (openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)) {{
        if (!m_gst_playbin || m_state != stRunning)
            return -1;
        eDebug("[{V9}] PAUSE enter");
        const GstStateChangeReturn rc = gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED);
        eDebug("[{V9}] PAUSE set_state result=%d", (int)rc);
        if (rc == GST_STATE_CHANGE_FAILURE)
            return -1;
        m_paused = true;
        m_currentTrickRatio = 1.0;
        return 0;
    }}'''
    return b[:match.start()]+safe+b[close:]


def repair_v8_pull(b):
    pattern=r'const\s+uint32_t\s+duration_ms\s*=\s*GST_CLOCK_TIME_IS_VALID\(dur\)\s*\?\s*\(uint32_t\)\(dur\s*/\s*GST_MSECOND\)\s*:\s*2500U\s*;'
    found=list(re.finditer(pattern,b))
    if len(found)!=1:raise RuntimeError('pullSubtitle: missing V8 duration expression')
    original=found[0]
    change='''/* Some MKV text buffers report a valid *zero* duration.
                 * A zero-length page is discarded by the renderer. */
                const uint32_t duration_ms =
                    (GST_CLOCK_TIME_IS_VALID(dur) && dur >= GST_MSECOND) ?
                    (uint32_t)(dur / GST_MSECOND) : 2500U;'''
    b=b[:original.start()]+change+b[original.end():]
    line='m_subtitle_sync_timer->start(20, true);'
    if b.count(line)!=1:raise RuntimeError('pullSubtitle: expected single V8 text-timer anchor')
    # Prints PTS, duration, and queue size to distinguish delivery from rendering.
    b=b.replace(line, f'''eDebug("[{V9}] text queued start=%u duration=%u end=%u pages=%zu widget=%d",
                    start_ms, duration_ms, end_ms, (size_t)m_subtitle_pages.size(),
                    m_subtitle_widget ? 1 : 0);
                {line}''',1)
    return b


def diagnose_sched(b):
    if 'getRawPlayPosition(running_pts)' not in b:raise RuntimeError('pushSubtitles: expected V8 native fallback')
    if 'm_subtitle_pages.lower_bound(decoder_ms)' not in b:raise RuntimeError('pushSubtitles: expected OpenBH map scheduler')
    if 'm_subtitle_widget->setPage(pango_page)' not in b:raise RuntimeError('pushSubtitles: expected Pango renderer')
    # Start a simple local file scheduler. Do not depend on the native decoder
    # clock stability gate: GStreamer subtitle PTS is in GST_FORMAT_TIME.
    # This is deliberately limited to files, preserving other existing paths.
    head=f'''
    /* {V9}: subtitles for local files use GstBuffer PTS / GStreamer time.
     * Avoid OpenBH's native decoder clock stability gate for this case.
     * Each page is consumed once (no repeated setPage calls). */
    if (openatv_v8_is_local_movie(m_ref, m_is_live, m_sourceinfo.is_audio)) {{
        if (m_currentSubtitleStream < 0 ||
            m_currentSubtitleStream >= (int)m_subtitleStreams.size())
            return;
        pts_t now_pts = 0;
        if (!openatv_v8_position(m_gst_playbin, now_pts)) {{
            static unsigned v9_clock_fail_count = 0;
            if (v9_clock_fail_count++ < 8)
                eDebug("[{V9}] subtitle clock unavailable, queue=%zu",
                    (size_t)m_subtitle_pages.size());
            m_subtitle_sync_timer->start(100, true);
            return;
        }}
        const int64_t now_ms = now_pts / 90;
        int32_t retry_ms = 1000;
        for (subtitle_pages_map_t::iterator it = m_subtitle_pages.begin();
             it != m_subtitle_pages.end();) {{
            const int64_t start_ms = it->second.start_ms;
            const int64_t end_ms = it->second.end_ms;
            if (end_ms <= now_ms || end_ms <= start_ms) {{
                eDebug("[{V9}] subtitle expired now=%lld start=%lld end=%lld",
                    (long long)now_ms, (long long)start_ms, (long long)end_ms);
                m_subtitle_pages.erase(it++);
                continue;
            }}
            if (now_ms + 20 < start_ms) {{
                int64_t delta = start_ms - now_ms;
                if (delta < retry_ms) retry_ms = (int32_t)delta;
                break;
            }}
            if (m_paused) {{
                retry_ms = 100;
                break;
            }}
            if (m_subtitle_widget) {{
                ePangoSubtitlePage page;
                gRGB color(0xD0, 0xD0, 0xD0);
                page.m_elements.push_back(ePangoSubtitlePageElement(
                    color, it->second.text.c_str()));
                page.m_show_pts = start_ms * 90;
                page.m_timeout = (int)(end_ms - now_ms);
                eDebug("[{V9}] RENDER subtitle now=%lld start=%lld end=%lld timeout=%d",
                    (long long)now_ms, (long long)start_ms, (long long)end_ms,
                    page.m_timeout);
                m_subtitle_widget->setPage(page);
            }} else {{
                eDebug("[{V9}] subtitle renderer missing now=%lld start=%lld",
                    (long long)now_ms, (long long)start_ms);
            }}
            m_subtitle_pages.erase(it++);
        }}
        if (retry_ms < 20) retry_ms = 20;
        if (retry_ms > 1000) retry_ms = 1000;
        m_subtitle_sync_timer->start(retry_ms, true);
        return;
    }}
'''
    return head+b


def patch(source):
    if V9 in source:
        for key in ('text queued start=','RENDER subtitle now=','PAUSE set_state result='):
            if key not in source:raise RuntimeError('Partial V9 patch marker found; use --restore')
        return source
    if V8 not in source or 'openatv_v8_is_local_movie' not in source:
        raise RuntimeError('V9 requires the V8 OpenATV local MoviePlayer source')
    out=replace_body(source,'pause',replace_v8_pause)
    out=replace_body(out,'pullSubtitle',repair_v8_pull)
    out=replace_body(out,'pushSubtitles',diagnose_sched)
    for key in ('PAUSE set_state result=','text queued start=','RENDER subtitle now='):
        if key not in out:raise RuntimeError('Final check missing '+key)
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    group=parser.add_mutually_exclusive_group(required=True)
    for cmd in ('check','apply','restore','diagnose'): group.add_argument('--'+cmd,action='store_true')
    args=parser.parse_args()
    if args.restore:
        if not BACKUP.exists():raise RuntimeError(f'Missing backup {BACKUP}')
        shutil.copy2(BACKUP,CPP);print('Restored',CPP)
        return
    if not CPP.is_file():raise RuntimeError('Run from Enigma2 checkout root (missing '+str(CPP)+')')
    orig=CPP.read_text(encoding='utf-8')
    if args.diagnose:
        print('V8 present:',V8 in orig,'V9 present:',V9 in orig,'bytes:',len(orig))
        for name in ('pause','pullSubtitle','pushSubtitles'):
            try:
                o,e=region(orig,name)
                print(name,'OK',orig.count('\n',0,o)+1,'-',orig.count('\n',0,e)+1)
            except RuntimeError as ex:print(name,ex)
        return
    updated=patch(orig)
    if updated==orig:
        print('Already patched: no changes')
        return
    if args.check:
        print('V9 CHECK OK; no files changed')
        delta=list(difflib.unified_diff(orig.splitlines(True),updated.splitlines(True),fromfile='servicemp3.cpp (old)',tofile='servicemp3.cpp (V9)',n=3))
        print(''.join(delta[:380]))
        return
    if not BACKUP.exists():shutil.copy2(CPP,BACKUP)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=CPP.parent,prefix='.v9_',delete=False) as out:
        out.write(updated);tmp=out.name
    try:
        os.chmod(tmp,CPP.stat().st_mode);os.replace(tmp,CPP)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print('Applied V9 to',CPP,'saved original:',BACKUP)
    print('Next: git diff --check ; bitbake -f -c compile enigma2')


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError) as err:
        print('ERROR:',err, file=__import__('sys').stderr)
        raise SystemExit(1)
