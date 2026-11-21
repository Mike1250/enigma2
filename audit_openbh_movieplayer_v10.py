#!/usr/bin/env python3
"""Read-only local-video audit for OpenBH eServiceMP3 V8/V9/V10.

Run at the root of the *actual* Enigma2 checkout:
  python3 audit_openbh_movieplayer_v10.py --root . --output movieplayer_audit.txt
Optional:
  python3 audit_openbh_movieplayer_v10.py --root . --log /path/to/enigma2.log
No modifications to the source tree or the receiver are performed.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import hashlib
from collections import Counter


def line_at(src: str, offset: int) -> int:
    return src.count('\n', 0, offset) + 1


def sig(src: str, path: Path) -> str:
    return f'{path}: {len(src):,} bytes sha256={hashlib.sha256(src.encode()).hexdigest()}'


def method(src: str, name: str):
    rx = re.compile(r'(?m)^[ \t]*(?:(?:RESULT|void|int|bool)\s+)?eServiceMP3::' + re.escape(name) + (r'\s*\(\s*\)' if name in ('pause','unpause','stop') else r'\s*\('))
    m = rx.search(src)
    if not m:
        return '', -1
    brace = src.find('{',m.end())
    if brace < 0 or brace > m.end()+700:
        return '', -1
    depth=0; state='c'; i=brace
    while i < len(src):
        c=src[i]; n=src[i+1] if i+1<len(src) else ''
        if state=='lc':
            if c=='\n':state='c'
        elif state=='bc':
            if c=='*' and n=='/':state='c';i+=1
        elif state in ('dq','sq'):
            if c=='\\':i+=1
            elif (state=='dq' and c=='"') or (state=='sq' and c=="'"):state='c'
        elif c=='/' and n=='/':state='lc';i+=1
        elif c=='/' and n=='*':state='bc';i+=1
        elif c=='"':state='dq'
        elif c=="'":state='sq'
        elif c=='{':depth+=1
        elif c=='}':
            depth-=1
            if depth==0: return src[m.start():i+1],line_at(src,m.start())
        i+=1
    return '', -1


def check(issues, level, label, details, source=None, needle=None):
    location=''
    if source and needle:
        x=source.find(needle)
        if x>=0:location=f' [line {line_at(source,x)}]'
    issues.append(f'[{level}] {label}{location}\n    {details}')


def main():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root',default='.',help='Enigma2 checkout root (default: .)')
    p.add_argument('--log',type=Path,help='Optional Enigma2 text log')
    p.add_argument('--binary',type=Path,help='Optional compiled enigma2 binary available on this computer')
    p.add_argument('--output',type=Path,help='Write report to this file (default: stdout only)')
    args=p.parse_args()
    root=Path(args.root).resolve()
    cpp=root/'lib/service/servicemp3.cpp'
    if not cpp.is_file():
        p.error(f'not found: {cpp}; run from your Enigma2 source checkout')
    src=cpp.read_text(errors='replace')
    lines=['OPENBH LOCAL MOVIEPLAYER AUDIT (read-only)',sig(src,cpp),
           'This checks actual source contents; runtime verification is separate.',
           'Source patches: '+', '.join(f'{v}={"YES" if v in src else "NO"}'
             for v in ['OPENBH_OPENATV_LOCAL_MOVIE_V8','OPENBH_OPENATV_LOCAL_MOVIE_V9','OPENBH_OPENATV_LOCAL_MOVIE_V10']),
           '']
    issues=[]
    bus,bl=method(src,'gstBusCall')
    pause,pl=method(src,'pause')
    unpause,ul=method(src,'unpause')
    start,sl=method(src,'start')
    enable,el=method(src,'enableSubtitles')
    apply,al=method(src,'applySubtitleStreamSwitch')
    pull,subl=method(src,'pullSubtitle')
    push,psl=method(src,'pushSubtitles')
    seek,sekl=method(src,'seekToImpl')
    stop,stopl=method(src,'stop')
    for name,loc in [('start',sl),('gstBusCall',bl),('pause',pl),('unpause',ul),('enableSubtitles',el),
                     ('applySubtitleStreamSwitch',al),('pullSubtitle',subl),('pushSubtitles',psl),
                     ('seekToImpl',sekl),('stop',stopl)]:
        if loc<0: check(issues,'WARNING',f'method {name} not identified', 'Source may differ from the audit baseline.')
    lines.append('STARTUP / RESUME')
    if 'initial bookmark seek pending=' in bus:
        pos=bus.find('initial bookmark seek pending=')
        tail=bus[pos:pos+950]
        if ('"openatv-v8-start-stage", NULL' in tail and 'GST_STATE_PLAYING' in tail):
            check(issues,'CONFIRMED BUG','V8 deferred bookmark stage is cleared immediately',
                  'Stage 2 is assigned after a successful bookmark seek but immediately overwritten by NULL; PLAYING is issued before the second ASYNC_DONE.',
                  src,'initial bookmark seek pending=')
        if 'initial bookmark seek rejected' in tail:
            check(issues,'CONFIRMED BUG','Misleading bookmark-seek rejection log',
                  'The "seek rejected" message runs even when seekToImpl succeeded.',src,'initial bookmark seek rejected')
    if 'local startup preroll started' in start and 'gst_element_set_state(m_gst_playbin, GST_STATE_PAUSED)' in start:
        check(issues,'REVIEW','V8 adds an independent startup preroll state',
              'Original gstBusCall also runs state transitions, media enumeration, and deferred seek; avoid duplicate PLAYING requests.',src,'local startup preroll started')
    if 'openatv_v8_position' in method(src,'getPlayPosition')[0]:
        lines.append('Counter: local files use gst_element_query_position directly (V8 override).')
    if 'GST_SEEK_FLAG_ACCURATE' in seek:
        lines.append('Seek: ACCURATE is referenced (confirm flag is used for local playback).')
    lines.append('\nSUBTITLE STREAM SELECTION / RENDERING')
    if 'openbhV10QueueText' in apply:
        if 'm_subtitle_switch_needs_flush = false' in apply and 'return;' in apply:
            check(issues,'HIGH','V10 skips original subtitle switch flush',
                  'Local branch queues current-text and returns; no equivalent forced input-selector pad commit found in this branch. Selected track may remain pending.',src,'m_subtitle_switch_needs_flush = false')
    if 'g_object_set' in enable and '"current-text", -1' in enable:
        lines.append('Subtitle enable: current-text reset is present (possibly guarded for nonlocal playback).')
    if 'm_subtitle_pages.erase(it++)' in push and 'subtitle expired' in push:
        check(issues,'REVIEW','V9 queue uses end-time-sorted subtitle map',
              'Early break on a future-start cue may delay overlapping cues whose end times order differently. Verify with samples.',src,'subtitle expired')
    if 'text queued start=' in pull:
        lines.append('Subtitle buffer handling: V9 text queue instrumentation is present.')
    if 'RENDER subtitle now=' in push:
        lines.append('Subtitle renderer instrumentation: V9 RENDER log is present.')
    if 'pango_subtitles_delay' in pull and 'if (openatv_v8_is_local_movie' in pull:
        check(issues,'MEDIUM','V8 local subtitle branch bypasses user subtitle timing settings',
              'Early return in pullSubtitle() prevents original pango_subtitles_delay and pango_subtitles_fps adjustments for local text cues.',src,'embedded subtitle type=')
    if '"ts-offset", -2LL * GST_SECOND' in src:
        check(issues,'REVIEW','Subsink -2-second offset still active',
              'Compare subsink-delivered PTS against gst_element_query_position; avoid applying multiple compensations.',src,'"ts-offset", -2LL * GST_SECOND')
    lines.append('\nPAUSE / RESUME / TEARDOWN')
    if 'openbhV10QueueState' in pause:
        check(issues,'HIGH','V10 marks pause complete before the state change completes',
              'm_paused=true is set before the asynchronous worker actually enters PAUSED. Other code queries m_paused immediately.',src,'PAUSE queued')
    if 'openbhV10QueueState' in unpause:
        check(issues,'HIGH','V10 marks resume complete before the state change completes',
              'm_paused=false is set before the asynchronous worker reaches PLAYING; rapid keypresses can race.',src,'RESUME queued')
    if 'gst_element_call_async(pipeline, openbhV10AsyncState' in src:
        check(issues,'REVIEW','Independent async state transitions need serialization',
              'Checking a serial before gst_element_set_state does not cancel an already-running state-change callback.',src,'gst_element_call_async(pipeline, openbhV10AsyncState')
    if 'openbh-v10-retired' in stop:
        lines.append('Stop: source retires the pipeline for queued V10 requests.')
    if 'stopWorker' in src and 'gst_element_set_state(playbin, GST_STATE_NULL)' in src:
        lines.append('Stop: the source already uses an asynchronous NULL-state worker; a stuck sink can hold this worker indefinitely.')
    ui=root/'lib/python/Plugins/Extensions/MediaPlayer/plugin.py'
    if ui.is_file():
        ui_text=ui.read_text(errors='replace')
        lines.append(f'\nUI source: {sig(ui_text,ui)}')
        for tag in ['MediaPlayerInfoBar','pauseEntry','def ok(','seekstate','unPauseService']:
            lines.append(f'  {tag}: {"found" if tag in ui_text else "absent"}')
        if not any(m in ui_text for m in ['OPENBH_LOCAL_PLAYBACK_V5','OPENBH_MEDIAPLAYER_TOGGLE']):
            lines.append('  UI-specific V3/V5 modifications were NOT positively identified by marker.')
    else:
        lines.append(f'\nUI source not found at {ui}: cannot audit Python MoviePlayer or OK toggle')
    h=root/'lib/service/servicemp3.h'
    lines.append(f'Header: {h} ({"present" if h.is_file() else "missing; cannot verify V8-V10 against header"})')
    if args.binary:
        if args.binary.is_file():
            b=args.binary.read_bytes()
            lines.append(f'\nBINARY {args.binary} sha256={hashlib.sha256(b).hexdigest()}')
            for v in [b'OPENBH_OPENATV_LOCAL_MOVIE_V8',b'OPENBH_OPENATV_LOCAL_MOVIE_V9',b'OPENBH_OPENATV_LOCAL_MOVIE_V10']:
                lines.append(f'  {v.decode()}: {"PRESENT" if v in b else "NOT FOUND"}')
        else: lines.append(f'\nBINARY not found: {args.binary}')
    if args.log:
        if args.log.is_file():
            log=args.log.read_text(errors='replace')
            keys=['OPENBH_OPENATV_LOCAL_MOVIE_V10','OPENBH_OPENATV_LOCAL_MOVIE_V9','RENDER subtitle','text queued','Main thread is busy','GST_STATE_CHANGE_FAILURE']
            lines.append(f'\nRUNTIME LOG: {args.log} ({len(log.splitlines())} lines)')
            for x in keys: lines.append(f'  {x}: {log.count(x)}')
            relevant=[x for x in log.splitlines() if any(term in x for term in keys)]
            lines.append('  Most recent relevant entries:')
            lines.extend('    '+x[:250] for x in relevant[-30:])
        else: lines.append(f'\nLOG not found: {args.log}')
    lines.append('\nFINDINGS:')
    lines.extend(issues if issues else ['No known V8-V10 bug signatures found. This does NOT verify runtime correctness.'])
    lines.append('\nNEXT: Verify /usr/bin/enigma2 includes V10 strings on the receiver; collect a fresh log covering subtitle selection and PAUSE. Do not stack further patches on unresolved V8/V10 states.')
    report='\n'.join(lines)+'\n'
    print(report)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(report)
        print('Wrote',args.output)

if __name__=='__main__':
    main()
