#!/usr/bin/env python3
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

import asyncio
import codecs
import io
import os
import re
import selectors
import signal
import sys
import termios
from contextlib import contextmanager
from functools import partial
from typing import Any, Callable, Dict, Generator, List, NamedTuple, Optional

from kitty.constants import is_macos
from kitty.fast_data_types import (
    close_tty, normal_tty, open_tty, parse_input_from_terminal, raw_tty
)
from kitty.key_encoding import (
    ALT, CTRL, PRESS, RELEASE, REPEAT, SHIFT, backspace_key, decode_key_event,
    enter_key
)
from kitty.typing import ImageManagerType, KeyEventType, Protocol
from kitty.utils import ScreenSizeGetter, screen_size_function, write_all

from .handler import Handler
from .operations import init_state, reset_state


class BinaryWrite(Protocol):

    def write(self, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass


def debug_write(*a: Any, **kw: Any) -> None:
    from base64 import standard_b64encode
    fobj = kw.pop('file', sys.stderr.buffer)
    buf = io.StringIO()
    kw['file'] = buf
    print(*a, **kw)
    stext = buf.getvalue()
    for i in range(0, len(stext), 256):
        chunk = stext[i:i + 256]
        text = b'\x1bP@kitty-print|' + standard_b64encode(chunk.encode('utf-8')) + b'\x1b\\'
        fobj.write(text)
    fobj.flush()


class Debug:

    fobj: Optional[BinaryWrite] = None

    def __call__(self, *a: Any, **kw: Any) -> None:
        kw['file'] = self.fobj or sys.stdout.buffer
        debug_write(*a, **kw)


debug = Debug()


class TermManager:

    def __init__(self, optional_actions: int = termios.TCSANOW) -> None:
        self.extra_finalize: Optional[str] = None
        self.optional_actions = optional_actions

    def set_state_for_loop(self, set_raw: bool = True) -> None:
        if set_raw:
            raw_tty(self.tty_fd, self.original_termios)
        write_all(self.tty_fd, init_state())

    def reset_state_to_original(self) -> None:
        normal_tty(self.tty_fd, self.original_termios)
        if self.extra_finalize:
            write_all(self.tty_fd, self.extra_finalize)
        write_all(self.tty_fd, reset_state())

    @contextmanager
    def suspend(self) -> Generator['TermManager', None, None]:
        self.reset_state_to_original()
        yield self
        self.set_state_for_loop()

    def __enter__(self) -> 'TermManager':
        self.tty_fd, self.original_termios = open_tty(False, self.optional_actions)
        self.set_state_for_loop(set_raw=False)
        return self

    def __exit__(self, *a: object) -> None:
        self.reset_state_to_original()
        close_tty(self.tty_fd, self.original_termios)
        del self.tty_fd, self.original_termios


LEFT, MIDDLE, RIGHT, FOURTH, FIFTH = 1, 2, 4, 8, 16
DRAG = REPEAT
bmap = {0: LEFT, 1: MIDDLE, 2: RIGHT}
MOTION_INDICATOR = 1 << 5
EXTRA_BUTTON_INDICATOR = 1 << 6
SHIFT_INDICATOR = 1 << 2
ALT_INDICATOR = 1 << 3
CTRL_INDICATOR = 1 << 4


class MouseEvent(NamedTuple):
    x: int
    y: int
    type: int
    buttons: int
    mods: int


def decode_sgr_mouse(text: str) -> MouseEvent:
    cb_, x_, y_ = text.split(';')
    m, y_ = y_[-1], y_[:-1]
    cb, x, y = map(int, (cb_, x_, y_))
    typ = RELEASE if m == 'm' else (DRAG if cb & MOTION_INDICATOR else PRESS)
    buttons = 0
    cb3 = cb & 3
    if cb3 != 3:
        if cb & EXTRA_BUTTON_INDICATOR:
            buttons |= FIFTH if cb3 & 1 else FOURTH
        else:
            buttons |= bmap[cb3]
    mods = 0
    if cb & SHIFT_INDICATOR:
        mods |= SHIFT
    if cb & ALT_INDICATOR:
        mods |= ALT
    if cb & CTRL_INDICATOR:
        mods |= CTRL
    return MouseEvent(x, y, typ, buttons, mods)


class UnhandledException(Handler):

    def __init__(self, tb: str) -> None:
        self.tb = tb

    def initialize(self) -> None:
        self.cmd.clear_screen()
        self.cmd.set_scrolling_region()
        self.cmd.set_cursor_visible(True)
        self.cmd.set_default_colors()
        self.write(self.tb.replace('\n', '\r\n'))
        self.write('\r\n')
        self.write('Press the Enter key to quit')

    def on_key(self, key_event: KeyEventType) -> None:
        if key_event.key == 'ENTER':
            self.quit_loop(1)

    def on_interrupt(self) -> None:
        self.quit_loop(1)
    on_eot = on_term = on_interrupt


class SignalManager:

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_winch: Callable,
        on_interrupt: Callable,
        on_term: Callable
    ) -> None:
        self.asycio_loop = loop
        self.on_winch, self.on_interrupt, self.on_term = on_winch, on_interrupt, on_term

    def __enter__(self) -> None:
        tuple(map(lambda x: self.asycio_loop.add_signal_handler(*x), (
            (signal.SIGWINCH, self.on_winch),
            (signal.SIGINT, self.on_interrupt),
            (signal.SIGTERM, self.on_term)
        )))

    def __exit__(self, *a: Any) -> None:
        tuple(map(self.asycio_loop.remove_signal_handler, (
            signal.SIGWINCH, signal.SIGINT, signal.SIGTERM)))


sanitize_bracketed_paste: str = '[\x03\x04\x0e\x0f\r\x07\x7f\x8d\x8e\x8f\x90\x9b\x9d\x9e\x9f]'


class Loop:

    def __init__(
        self,
        sanitize_bracketed_paste: str = sanitize_bracketed_paste,
        optional_actions: int = termios.TCSADRAIN
    ):
        if is_macos:
            # On macOS PTY devices are not supported by the KqueueSelector and
            # the PollSelector is broken, causes 100% CPU usage
            self.asycio_loop: asyncio.AbstractEventLoop = asyncio.SelectorEventLoop(selectors.SelectSelector())
            asyncio.set_event_loop(self.asycio_loop)
        else:
            self.asycio_loop = asyncio.get_event_loop()
        self.return_code = 0
        self.optional_actions = optional_actions
        self.read_buf = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('ignore')
        try:
            self.iov_limit = max(os.sysconf('SC_IOV_MAX') - 1, 255)
        except Exception:
            self.iov_limit = 255
        self.parse_input_from_terminal = partial(parse_input_from_terminal, self._on_text, self._on_dcs, self._on_csi, self._on_osc, self._on_pm, self._on_apc)
        self.ebs_pat = re.compile('([\177\r\x03\x04])')
        self.in_bracketed_paste = False
        self.sanitize_bracketed_paste = bool(sanitize_bracketed_paste)
        if self.sanitize_bracketed_paste:
            self.sanitize_ibp_pat = re.compile(sanitize_bracketed_paste)

    def _read_ready(self, handler: Handler, fd: int) -> None:
        try:
            bdata = os.read(fd, io.DEFAULT_BUFFER_SIZE)
        except BlockingIOError:
            return
        if not bdata:
            raise EOFError('The input stream is closed')
        data = self.decoder.decode(bdata)
        if self.read_buf:
            data = self.read_buf + data
        self.read_buf = data
        self.handler = handler
        try:
            self.read_buf = self.parse_input_from_terminal(self.read_buf, self.in_bracketed_paste)
        except Exception:
            self.read_buf = ''
            raise
        finally:
            del self.handler

    # terminal input callbacks {{{
    def _on_text(self, text: str) -> None:
        if self.in_bracketed_paste and self.sanitize_bracketed_paste:
            text = self.sanitize_ibp_pat.sub('', text)

        for chunk in self.ebs_pat.split(text):
            if len(chunk) == 1:
                if chunk == '\r':
                    self.handler.on_key(enter_key)
                elif chunk == '\177':
                    self.handler.on_key(backspace_key)
                elif chunk == '\x03':
                    self.handler.on_interrupt()
                elif chunk == '\x04':
                    self.handler.on_eot()
                else:
                    self.handler.on_text(chunk, self.in_bracketed_paste)
            elif chunk:
                self.handler.on_text(chunk, self.in_bracketed_paste)

    def _on_dcs(self, dcs: str) -> None:
        if dcs.startswith('@kitty-cmd'):
            import json
            self.handler.on_kitty_cmd_response(json.loads(dcs[len('@kitty-cmd'):]))
        elif dcs.startswith('1+r'):
            from binascii import unhexlify
            vals = dcs[3:].split(';')
            for q in vals:
                parts = q.split('=', 1)
                try:
                    name, val = parts[0], unhexlify(parts[1]).decode('utf-8', 'replace')
                except Exception:
                    continue
                self.handler.on_capability_response(name, val)

    def _on_csi(self, csi: str) -> None:
        q = csi[-1]
        if q in 'mM':
            if csi.startswith('<'):
                # SGR mouse event
                try:
                    ev = decode_sgr_mouse(csi[1:])
                except Exception:
                    pass
                else:
                    self.handler.on_mouse(ev)
        elif q in 'u~ABCDEHFPQRS':
            if csi == '200~':
                self.in_bracketed_paste = True
                return
            elif csi == '201~':
                self.in_bracketed_paste = False
                return
            try:
                k = decode_key_event(csi[:-1], q)
            except Exception:
                pass
            else:
                if k.matches('ctrl+c'):
                    self.handler.on_interrupt()
                    return
                if k.matches('ctrl+d'):
                    self.handler.on_eot()
                    return
                self.handler.on_key_event(k)

    def _on_pm(self, pm: str) -> None:
        pass

    def _on_osc(self, osc: str) -> None:
        m = re.match(r'(\d+);', osc)
        if m is not None:
            code = int(m.group(1))
            rest = osc[m.end():]
            if code == 52:
                where, rest = rest.partition(';')[::2]
                from_primary = 'p' in where
                from base64 import standard_b64decode
                self.handler.on_clipboard_response(standard_b64decode(rest).decode('utf-8'), from_primary)

    def _on_apc(self, apc: str) -> None:
        if apc.startswith('G'):
            if self.handler.image_manager is not None:
                self.handler.image_manager.handle_response(apc)
    # }}}

    def _write_ready(self, handler: Handler, fd: int) -> None:
        if len(self.write_buf) > self.iov_limit:
            self.write_buf[self.iov_limit - 1] = b''.join(self.write_buf[self.iov_limit - 1:])
            del self.write_buf[self.iov_limit:]
        sizes = tuple(map(len, self.write_buf))
        total_size = sum(sizes)
        if total_size:
            try:
                written = os.writev(fd, self.write_buf)
            except BlockingIOError:
                return
            if not written:
                raise EOFError('The output stream is closed')
        else:
            written = 0
        if written >= total_size:
            self.write_buf: List[bytes] = []
            self.asycio_loop.remove_writer(fd)
            self.waiting_for_writes = False
        else:
            consumed = 0
            for i, buf in enumerate(self.write_buf):
                if not written:
                    break
                if len(buf) <= written:
                    written -= len(buf)
                    consumed += 1
                    continue
                self.write_buf[i] = buf[written:]
                break
            del self.write_buf[:consumed]

    def quit(self, return_code: Optional[int] = None) -> None:
        if return_code is not None:
            self.return_code = return_code
        self.asycio_loop.stop()

    def loop_impl(self, handler: Handler, term_manager: TermManager, image_manager: Optional[ImageManagerType] = None) -> Optional[str]:
        self.write_buf = []
        tty_fd = term_manager.tty_fd
        tb = None
        self.waiting_for_writes = True

        def schedule_write(data: bytes) -> None:
            self.write_buf.append(data)
            if not self.waiting_for_writes:
                self.asycio_loop.add_writer(tty_fd, self._write_ready, handler, tty_fd)
                self.waiting_for_writes = True

        def handle_exception(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
            nonlocal tb
            loop.stop()
            tb = context['message']
            exc = context.get('exception')
            if exc is not None:
                import traceback
                tb += '\n' + ''.join(traceback.format_exception(exc.__class__, exc, exc.__traceback__))

        self.asycio_loop.set_exception_handler(handle_exception)
        handler._initialize(self._get_screen_size(), term_manager, schedule_write, self, debug, image_manager)
        with handler:
            self.asycio_loop.add_reader(
                    tty_fd, self._read_ready, handler, tty_fd)
            self.asycio_loop.add_writer(
                    tty_fd, self._write_ready, handler, tty_fd)
            self.asycio_loop.run_forever()
            self.asycio_loop.remove_reader(tty_fd)
            if self.waiting_for_writes:
                self.asycio_loop.remove_writer(tty_fd)
        return tb

    def loop(self, handler: Handler) -> None:
        tb: Optional[str] = None

        def _on_sigwinch() -> None:
            self._get_screen_size.changed = True
            handler.screen_size = self._get_screen_size()
            handler.on_resize(handler.screen_size)

        signal_manager = SignalManager(self.asycio_loop, _on_sigwinch, handler.on_interrupt, handler.on_term)
        with TermManager(self.optional_actions) as term_manager, signal_manager:
            self._get_screen_size: ScreenSizeGetter = screen_size_function(term_manager.tty_fd)
            image_manager = None
            if handler.image_manager_class is not None:
                image_manager = handler.image_manager_class(handler)
            try:
                tb = self.loop_impl(handler, term_manager, image_manager)
            except Exception:
                import traceback
                tb = traceback.format_exc()

            term_manager.extra_finalize = b''.join(self.write_buf).decode('utf-8')
            if tb is not None:
                self.return_code = 1
                self._report_error_loop(tb, term_manager)

    def _report_error_loop(self, tb: str, term_manager: TermManager) -> None:
        self.loop_impl(UnhandledException(tb), term_manager)
