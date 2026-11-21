#!/usr/bin/env python3
"""Restore pause/resume of local GStreamer video in OpenBH Enigma2.

Usage (run inside your enigma2 Git checkout):
    python3 /path/to/fix_openbh_local_video_pause_resume.py

Only edits:
  lib/service/servicemp3.cpp
  lib/python/Screens/InfoBarGenerics.py

The C++ change bypasses the old trickSeek() flush/seek path ONLY for ordinary
1.0x local-video pause/resume. Nonlocal streams, audio-only services and
trick-play retain the original implementation. The UI fix makes OK resume
local MoviePlayer playback when already paused.

The script performs bounded edits, checks for markers, and refuses unexpected
source structure; it does NOT commit, push or build.
"""

from pathlib import Path
import re
import sys

CPP_PATH = Path('lib/service/servicemp3.cpp')
PY_PATH = Path('lib/python/Screens/InfoBarGenerics.py')
CPP_MARKER = 'OPENBH_LOCAL_MOVIE_PAUSE_RESUME_V1'
PY_MARKER = 'OPENBH_MOVIE_OK_RESUME_V1'


def function_span(source: str, signature: str):
    match = list(re.finditer(r'(?m)^' + re.escape(signature) + r'\s*\n\{', source))
    if len(match) != 1:
        raise RuntimeError(f'Expected exactly one function: {signature}; found {len(match)}')
    open_brace = source.find('{', match[0].start(), match[0].end() + 1)
    depth = 0
    for i in range(open_brace, len(source)):
        ch = source[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return open_brace, i
    raise RuntimeError(f'Unclosed function body: {signature}')


def edit_cpp(source: str) -> str:
    if CPP_MARKER in source:
        return source

    # The name is intentionally checked here to avoid patching the wrong file.
    for action, state in [('pause', 'PAUSED'), ('unpause', 'PLAYING')]:
        signature = f'RESULT eServiceMP3::{action}()'
        body_start, body_end = function_span(source, signature)
        body = source[body_start + 1:body_end]
        early_guard = re.search(
            r'\n\s*if\s*\(\s*!m_gst_playbin\s*\|\|\s*m_state\s*!=\s*stRunning\s*\)\s*\n\s*return\s+-1\s*;',
            body)
        if early_guard is None:
            raise RuntimeError(f'Unexpected {signature} body: expected pipeline guard absent')
        if 'trickSeek(' not in body:
            raise RuntimeError(f'Unexpected {signature} body: expected trickSeek absent')

        extra = (
            '\n\n'
            f'\t/* [{CPP_MARKER}] Ordinary local video only: pause/resume is\n'
            '\t * a GStreamer state transition, NOT a flushing seek. The old\n'
            '\t * trickSeek() resume fallback can restart preroll on a keyframe\n'
            '\t * and leave the Amlogic decoder stuck after a normal pause.\n'
            '\t * Keep legacy behavior for network streams, audio-only, and\n'
            '\t * fast/slow/reverse playback. */\n'
            '\tconst bool openbh_local_movie = !m_sourceinfo.is_audio &&\n'
            '\t\t!m_ref.path.empty() &&\n'
            '\t\t(m_ref.path[0] == \'/\' || m_ref.path.compare(0, 7, "file://") == 0);\n'
            '\tif (openbh_local_movie && m_currentTrickRatio == 1.0)\n'
            '\t{\n'
            f'\t\teDebug("[{CPP_MARKER}] local movie {action}: GST_STATE_{state}");\n'
            f'\t\tGstStateChangeReturn result = gst_element_set_state(m_gst_playbin, GST_STATE_{state});\n'
            '\t\tif (result == GST_STATE_CHANGE_FAILURE)\n'
            '\t\t{\n'
            f'\t\t\teWarning("[{CPP_MARKER}] failed to {action} local movie");\n'
            '\t\t\treturn -1;\n'
            '\t\t}\n'
            f'\t\tsetHDAudioAuxState(m_gst_playbin, GST_STATE_{state});\n'
            '\t\tm_prev_decoder_time = -1;\n'
            '\t\tm_decoder_time_valid_state = 0;\n'
            '\t\treturn 0;\n'
            '\t}\n'
        )
        # Always insert after the guard, never replace unrelated code.
        insertion_at = body_start + 1 + early_guard.end()
        source = source[:insertion_at] + extra + source[insertion_at:]
    return source


def edit_python(source: str) -> str:
    if PY_MARKER in source:
        return source
    # Restrict fix to this exact method and existing pause branch.
    start_matches = list(re.finditer(r'(?m)^\tdef okButton\(self\):\s*$', source))
    if len(start_matches) != 1:
        raise RuntimeError(f'Expected one InfoBarSeek.okButton(), found {len(start_matches)}')
    start = start_matches[0].start()
    next_def = re.search(r'(?m)^\tdef \w+\(self[^\n]*\):', source[start_matches[0].end():])
    if next_def is None:
        raise RuntimeError('Unable to find method after okButton')
    end = start_matches[0].end() + next_def.start()
    method = source[start:end]
    old = '\t\telif self.seekstate == self.SEEK_STATE_PAUSE:\n\t\t\tself.pauseService()'
    if method.count(old) != 1:
        raise RuntimeError('Unexpected okButton body; expected paused branch missing')
    new = ('\t\telif self.seekstate == self.SEEK_STATE_PAUSE:\n'
           f'\t\t\t# {PY_MARKER}: OK resumes a paused local MoviePlayer.\n'
           '\t\t\tif isMoviePlayerInfoBar(self):\n'
           '\t\t\t\tself.unPauseService()\n'
           '\t\t\telse:\n'
           '\t\t\t\tself.pauseService()')
    source = source[:start] + method.replace(old, new, 1) + source[end:]
    # The same pause/play key should toggle back to PLAY in MoviePlayer,
    # independently of the global configuration's step/last behavior.
    target = '\tdef playpauseService(self):\n'
    if source.count(target) != 1:
        raise RuntimeError('Expected exactly one playpauseService method')
    source = source.replace(target, target + (
        '\t\t# ' + PY_MARKER + ': MoviePlayer toggle resumes instead of stepping.\n'
        '\t\tif isMoviePlayerInfoBar(self) and self.seekstate == self.SEEK_STATE_PAUSE:\n'
        '\t\t\tself.unPauseService()\n'
        '\t\t\treturn\n'
    ), 1)
    return source


def main():
    if not CPP_PATH.is_file() or not PY_PATH.is_file():
        raise RuntimeError('Run from the root of your Enigma2 Git checkout (amlogic).')
    originals = {
        CPP_PATH: CPP_PATH.read_text(encoding='utf-8'),
        PY_PATH: PY_PATH.read_text(encoding='utf-8'),
    }
    modified = {
        CPP_PATH: edit_cpp(originals[CPP_PATH]),
        PY_PATH: edit_python(originals[PY_PATH]),
    }
    changed = [path for path in modified if modified[path] != originals[path]]
    if not changed:
        print('Already applied: no changes necessary.')
        return
    for path in changed:
        path.write_text(modified[path], encoding='utf-8')
        print(f'Updated: {path}')
    print('\nNo driver/DVB/other playback files touched.')
    print('Review: git diff --check && git diff --stat && git diff -- lib/service/servicemp3.cpp lib/python/Screens/InfoBarGenerics.py')
    print('No commit/push/build has been performed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
