import asyncio
import sys
import unittest
from unittest.mock import Mock, patch

import runtime_compat


class RuntimeCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows transport')
    def test_reset_during_proactor_cleanup_closes_socket_and_detaches(self):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        async def scenario():
            loop = asyncio.get_running_loop()
            reported = []
            old = lambda loop, context: reported.append(context)
            loop.set_exception_handler(old)
            transport = object.__new__(_ProactorBasePipeTransport)
            transport._protocol = Mock()
            transport._called_connection_lost = False
            transport._sock = Mock()
            transport._sock.fileno.return_value = 7
            error = ConnectionResetError('reset')
            error.winerror = 10054
            transport._sock.shutdown.side_effect = error
            sock = transport._sock
            transport._server = Mock()
            owner = transport._server
            async with runtime_compat.lifespan(None):
                loop.call_soon(transport._call_connection_lost, None)
                await asyncio.sleep(0)
            self.assertFalse(reported)
            sock.close.assert_called_once()
            owner._detach.assert_called_once_with(transport)
            self.assertTrue(transport._called_connection_lost)
            self.assertIs(loop.get_exception_handler(), old)
        asyncio.run(scenario())

    def test_unrelated_errors_are_forwarded(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            old = Mock()
            loop.set_exception_handler(old)
            async with runtime_compat.lifespan(None):
                handler = loop.get_exception_handler()
                context = {'message':'application error','exception':ValueError('test')}
                handler(loop, context)
                old.assert_called_once_with(loop, context)
        asyncio.run(scenario())

    def test_non_windows_loop_is_unchanged(self):
        async def scenario():
            loop=asyncio.get_running_loop()
            old=loop.get_exception_handler()
            with patch.object(runtime_compat.sys,'platform','linux'):
                async with runtime_compat.lifespan(None):
                    self.assertIs(loop.get_exception_handler(),old)
        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows transport')
    def test_cleanup_reset_does_not_hide_an_earlier_protocol_error(self):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        async def scenario():
            loop=asyncio.get_running_loop(); reported=[]
            loop.set_exception_handler(lambda loop, context: reported.append(context))
            transport=object.__new__(_ProactorBasePipeTransport)
            transport._called_connection_lost=False
            transport._protocol=Mock()
            transport._protocol.connection_lost.side_effect=ValueError('real protocol failure')
            transport._sock=Mock(); sock=transport._sock
            sock.fileno.return_value=7
            error=ConnectionResetError('reset'); error.winerror=10054
            sock.shutdown.side_effect=error
            transport._server=None
            async with runtime_compat.lifespan(None):
                loop.call_soon(transport._call_connection_lost,None)
                await asyncio.sleep(0)
            sock.close.assert_called_once()
            self.assertEqual(len(reported),1)
            self.assertIsInstance(reported[0]['exception'].__context__,ValueError)
        asyncio.run(scenario())
