# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Piped standard input on Windows. See :mod:`pb_cli.piped` for the why.

The pipe a shell gives a command is made by ``CreatePipe``, which never opens it
for overlapped I/O, so nothing an event loop has will wait on it: the proactor
wants an overlapped handle, the handle itself is signalled whenever no I/O is
pending rather than when data arrives, and ``ReOpenFile`` with
``FILE_FLAG_OVERLAPPED`` is refused with ``ERROR_PIPE_BUSY``. Measured, all
three.

What is left is libuv's answer, the one every Node program reading a piped
stdin on Windows runs on: **a thread does a zero-byte ``ReadFile``.** On a pipe
that blocks until data arrives or the writer closes, and takes nothing, so the
bytes are then read on the loop by a read that cannot block. And it can be
released: ``CancelSynchronousIo`` aborts it with ``ERROR_OPERATION_ABORTED`` and
leaves the pipe working. Measured, both -- which is what this has over a thread
parked in a real read, and the opposite of what the console does (see
:mod:`pb_cli.console.windows`, where cancelling a read breaks the console).

**One race, closed the way libuv closes it.** A thread that has said it is about
to read but has not yet entered ``ReadFile`` has no I/O to cancel, and
``CancelSynchronousIo`` says ``ERROR_NOT_FOUND``. Worse, it may be doing some
*other* synchronous I/O in that window -- a garbage-collection finalizer
writing to a full stderr pipe -- and then the cancel lands on that, reports
success, and the read it was meant for starts afterwards with nothing left to
cancel it. So a success proves nothing either. Releasing the thread keeps
cancelling, yielding the processor in between with ``SwitchToThread``, until
the thread itself says it is past the read -- whatever each cancel returned.
That is what libuv's ``uv__pipe_interrupt_read`` does, and why. The loop runs
only while a wait is being cancelled, and only for the instructions between
saying and reading -- it waits for nothing, so it is not the poll rule 7
forbids. A thread held up in that window by something slow and not
cancellable, such as a finalizer taking a lock, keeps it spinning for as long;
there is no way to wait for a thread to reach a system call.

That finalizer case has one corner this cannot close. When the I/O a cancel
lands on ends and the thread starts its read *the instant* it does, Windows can
make the ``CancelSynchronousIo`` call itself wait for that read to finish --
measured with plain threads and no code from here, in about one run in fifteen
with nothing in between the two, and never with fifty microseconds between
them. The release is then stuck inside the call, not in its loop, until stdin
has data or ends. Only blocking I/O run by a finalizer in that window leads
there; libuv has the same exposure.

**While the zero-byte read is waiting, the pipe is busy.** A synchronous handle
lets one call at a time through, so ``PeekNamedPipe`` and
``NtQueryInformationFile`` on it from another thread, and a child process
started with it as stdin, wait until data arrives or the writer closes --
measured, a Python child did not even start. Nothing here does any of those
during a wait, and nothing can be done about it short of overlapped I/O, which
this pipe does not have; libuv has the same limit. So a subprocess started
while a session is running must be given a stdin of its own, such as
``subprocess.DEVNULL``, never inherit this one.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import msvcrt
import os
import threading
from typing import Final

import anyio
import anyio.to_thread

from pb_cli import piped

__all__ = ["reader"]

logger = logging.getLogger(__name__)

_FILE_TYPE_DISK: Final = 0x0001
_FILE_TYPE_PIPE: Final = 0x0003
_THREAD_TERMINATE: Final = 0x0001
"""The access ``CancelSynchronousIo`` needs to the thread it cancels."""

_ERROR_BROKEN_PIPE: Final = 109
_ERROR_NOT_FOUND: Final = 1168

# ctypes rather than pywin32, for the reason given in pb_cli.console.windows.
_kernel32: Final = ctypes.WinDLL("kernel32", use_last_error=True)

_GetFileType: Final = _kernel32.GetFileType
_GetFileType.argtypes = (wintypes.HANDLE,)
_GetFileType.restype = wintypes.DWORD

_PeekNamedPipe: Final = _kernel32.PeekNamedPipe
_PeekNamedPipe.argtypes = (
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
)
_PeekNamedPipe.restype = wintypes.BOOL

_ReadFile: Final = _kernel32.ReadFile
_ReadFile.argtypes = (
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPVOID,
)
_ReadFile.restype = wintypes.BOOL

_OpenThread: Final = _kernel32.OpenThread
_OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_OpenThread.restype = wintypes.HANDLE

_GetCurrentThreadId: Final = _kernel32.GetCurrentThreadId
_GetCurrentThreadId.argtypes = ()
_GetCurrentThreadId.restype = wintypes.DWORD

_CancelSynchronousIo: Final = _kernel32.CancelSynchronousIo
_CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
_CancelSynchronousIo.restype = wintypes.BOOL

_CloseHandle: Final = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL

_SwitchToThread: Final = _kernel32.SwitchToThread
_SwitchToThread.argtypes = ()
_SwitchToThread.restype = wintypes.BOOL

_FILE_MODE_INFORMATION: Final = 16
"""``FileModeInformation``, the class that says how a handle was opened."""

_FILE_SYNCHRONOUS_IO: Final = 0x0010 | 0x0020
"""``FILE_SYNCHRONOUS_IO_ALERT`` and ``FILE_SYNCHRONOUS_IO_NONALERT``.

A handle with neither was opened for overlapped I/O.
"""


class _IoStatusBlock(ctypes.Structure):
    _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))


_ntdll: Final = ctypes.WinDLL("ntdll")

_NtQueryInformationFile: Final = _ntdll.NtQueryInformationFile
_NtQueryInformationFile.argtypes = (
    wintypes.HANDLE,
    ctypes.POINTER(_IoStatusBlock),
    wintypes.LPVOID,
    wintypes.ULONG,
    ctypes.c_int,
)
_NtQueryInformationFile.restype = wintypes.LONG


def _synchronous(handle: int) -> bool:
    """Whether a handle was opened for ordinary, blocking I/O.

    The pipe a shell makes always is. One handed over by something else --
    another program that made the pipe itself -- may be overlapped, and then a
    read without an ``OVERLAPPED`` is not a read that blocks: ``ReadFile``
    documents that it may report completion at once. The zero-byte wait would
    return immediately, every time, and the wait would become a spin. libuv
    asks the same question for the same reason.

    Args:
        handle: The handle to ask about.

    Returns:
        True if every read on it blocks until it completes.
    """
    status = _IoStatusBlock()
    mode = wintypes.ULONG()
    result = _NtQueryInformationFile(
        handle,
        ctypes.byref(status),
        ctypes.byref(mode),
        ctypes.sizeof(mode),
        _FILE_MODE_INFORMATION,
    )

    # A negative NTSTATUS is an error. Not knowing is treated as not
    # synchronous, since guessing wrong the other way is a spinning core.
    return result >= 0 and bool(mode.value & _FILE_SYNCHRONOUS_IO)


class _ZeroReadPipe:
    """An anonymous pipe, waited on by a zero-byte read in a thread."""

    def __init__(self, fd: int, handle: int) -> None:
        """Hold the descriptor and the handle behind it.

        Args:
            fd: Standard input, for reading.
            handle: The same, for waiting and asking.
        """
        self._fd = fd
        self._handle = handle
        self._nothing = ctypes.create_string_buffer(1)
        """Somewhere for a read of no bytes to put them."""

        self._lock = threading.Lock()
        """Guards the two below, and keeps the thread's handle open while it
        is being cancelled: the thread clears it under this lock before
        closing it."""

        self._waiter: int | None = None
        """The thread in the read, or about to be."""

        self._stopping = False
        """Set once the wait is over, so a thread not yet started skips it."""

    async def read(self) -> bytes:
        """Wait until there is input, then take it.

        Returns:
            The bytes, or empty once the writer has closed its end and
            everything it wrote has been read.
        """

        while True:
            waiting = self._waiting()
            if waiting is None:
                return b""

            if waiting:
                # No more than is there, so the read cannot block.
                return os.read(self._fd, min(waiting, piped.READ_LIMIT))

            # A wait can end with nothing to read -- cancelled, or woken by a
            # write of nothing -- so ask again rather than assume.
            await self._wait()

    def _waiting(self) -> int | None:
        """Ask how much is in the pipe, without taking any of it.

        Returns:
            The byte count, or None once the writer has gone and the pipe is
            empty.

        Raises:
            OSError: If the pipe cannot be asked at all.
        """
        count = wintypes.DWORD()

        if not _PeekNamedPipe(self._handle, None, 0, None, ctypes.byref(count), None):
            error = ctypes.get_last_error()
            if error == _ERROR_BROKEN_PIPE:
                return None

            raise ctypes.WinError(error)

        return count.value

    async def _wait(self) -> None:
        """Wait until the pipe has data or has ended, or this task is cancelled.

        The same shape as the console's wait: the thread is inside the task
        tree, because whichever way this returns -- data, the end of the pipe,
        or a cancellation -- it does not return until the thread has.
        """

        with self._lock:
            self._stopping = False

        async with anyio.create_task_group() as tg:

            async def watch() -> None:
                # abandon_on_cancel=False: a cancel waits for the thread, which
                # is safe because the finally below is about to release it.
                await anyio.to_thread.run_sync(self._block, abandon_on_cancel=False)
                tg.cancel_scope.cancel()

            tg.start_soon(watch)

            try:
                await anyio.sleep_forever()

            finally:
                # Synchronous, so a cancelled scope cannot skip it.
                self._release()

    def _block(self) -> None:
        """Block in a zero-byte read until the pipe has something to say.

        Runs in a worker thread. ctypes releases the interpreter lock around
        the read, so nothing else is held up by it. How the read ended is not
        looked at: :meth:`read` asks the pipe afresh either way.
        """
        me = _OpenThread(_THREAD_TERMINATE, False, _GetCurrentThreadId())
        if not me:
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            with self._lock:
                if self._stopping:
                    return

                self._waiter = me

            try:
                _ReadFile(
                    self._handle, self._nothing, 0, ctypes.byref(wintypes.DWORD()), None
                )
            finally:
                with self._lock:
                    self._waiter = None
        finally:
            _CloseHandle(me)

    def _release(self) -> None:
        """Get the thread out of its read, or stop it from starting one.

        Returns only once the thread has said it is past the read: a cancel
        that succeeded may have hit some other I/O of the thread's instead.
        See the module docstring. Never raises, because it runs in a finally
        during cancellation, and raising there would lose the cancellation and
        still leave the thread in its read.
        """

        warned = False

        while True:
            error = 0

            with self._lock:
                self._stopping = True

                if self._waiter is None:
                    # Not started, so it will see _stopping; or done.
                    return

                if not _CancelSynchronousIo(self._waiter):
                    error = ctypes.get_last_error()

            if error not in (0, _ERROR_NOT_FOUND) and not warned:
                # Not expected: the handle was opened with the access this
                # needs. Retrying is still the only way out. Said outside the
                # lock, because logging can block on a full stderr pipe, and
                # the thread needs the lock to say it is done.
                logger.warning(
                    "cancelling the stdin wait failed: %s", ctypes.FormatError(error)
                )
                warned = True

            # Let the thread run the few instructions into the read, where the
            # next cancel will find it, or out of it, where it clears _waiter.
            _SwitchToThread()


def reader(fd: int) -> piped.Piped | None:
    """Choose how to read standard input on Windows.

    Args:
        fd: Standard input, already known not to be a terminal.

    Returns:
        A reader, or None for something this does not read -- a console that
        isatty missed, the NUL device, which has nothing to say anyway, a
        handle opened for overlapped I/O, which nothing here can wait on, or a
        pipe that cannot be read from at all.
    """
    handle = msvcrt.get_osfhandle(fd)
    kind = _GetFileType(handle)

    if not _synchronous(handle):
        logger.info("standard input was opened for overlapped I/O; not forwarding it")
        return None

    if kind == _FILE_TYPE_PIPE:
        pipe = _ZeroReadPipe(fd, handle)

        try:
            # Asked once now, so that a pipe with nothing readable about it --
            # the write end, as in pb run x.py 0<&1 | more -- is left alone
            # rather than ending the session on its first read.
            pipe._waiting()  # pyright: ignore[reportPrivateUsage]
        except OSError as exc:
            logger.info("standard input is a pipe that cannot be read: %s", exc)
            return None

        return pipe

    if kind == _FILE_TYPE_DISK:
        return piped.ready(fd)

    return None
