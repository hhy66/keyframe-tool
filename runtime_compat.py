"""Windows Proactor socket-cleanup compatibility without hiding application errors."""
import asyncio
from contextlib import asynccontextmanager
import logging
import sys


def _finish_reset_cleanup(context):
    error = context.get('exception')
    handle = context.get('handle')
    callback = getattr(handle, '_callback', None)
    transport = getattr(callback, '__self__', None)
    if not (isinstance(error, ConnectionResetError)
            and getattr(error, 'winerror', None) == 10054
            and getattr(callback, '__module__', '') == 'asyncio.proactor_events'
            and getattr(callback, '__name__', '') == '_call_connection_lost'
            and transport is not None
            and not getattr(transport, '_called_connection_lost', False)
            and getattr(transport, '_sock', None) is not None):
        return False
    # Confirm the exception came from the cleanup callback, not another caller.
    traceback = error.__traceback__
    matched = False
    while traceback:
        if (traceback.tb_frame.f_code.co_name == '_call_connection_lost'
                and traceback.tb_frame.f_locals.get('self') is transport):
            matched = True
        traceback = traceback.tb_next
    if not matched:
        return False
    # In affected Python versions shutdown() raises before close()/server detach.
    # Finish those remaining steps; do not invoke protocol.connection_lost twice.
    sock = getattr(transport, '_sock', None)
    if sock is not None:
        sock.close()
        transport._sock = None
    owner = getattr(transport, '_server', None)
    if owner is not None:
        owner._detach(transport)
        transport._server = None
    transport._called_connection_lost = True
    # A reset raised in finally may wrap a real protocol/application failure.
    # Complete cleanup but keep that exception chain visible to the old handler.
    return error.__context__ is None and error.__cause__ is None


@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    def handler(active_loop, context):
        try:
            if _finish_reset_cleanup(context):
                logging.getLogger(__name__).debug('Closed a Windows socket already reset by its peer')
                return
        except Exception:
            # An unexpected cleanup failure remains visible with the original error.
            logging.getLogger(__name__).exception('Unable to finish Windows connection cleanup')
        if previous is not None:
            previous(active_loop, context)
        else:
            active_loop.default_exception_handler(context)
    installed = sys.platform == 'win32'
    if installed:
        loop.set_exception_handler(handler)
    try:
        yield
    finally:
        if installed and loop.get_exception_handler() is handler:
            loop.set_exception_handler(previous)
