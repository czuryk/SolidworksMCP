"""Reader diagnostics preserve native failures and equivalent geometry access."""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation.runtime import RuntimeState
from solidworks_mcp.automation.tessellation_reader import TessellationReader
from solidworks_mcp.automation.com_utils import com_get, command_scope
from solidworks_mcp.shape_program import ProgramError


class RpcError(Exception):
    hresult=-2147023174


class ReaderTests(unittest.TestCase):
    def test_rpc_is_not_retried_or_replaced_by_default(self):
        class Broken:
            calls=0
            def GetVertexPoint(self,index):
                self.calls+=1
                raise RpcError('original RPC cause')
            def _FlagAsMethod(self,name):
                raise AssertionError('RPC must not be retried')
        source=Broken()
        with self.assertRaises(RpcError):com_get(source,'GetVertexPoint',12,default=[])
        self.assertEqual(source.calls,1)
        runtime=RuntimeState();runtime.record_progress('tessellation_read',completed=250,total=1000,unit='facets')
        reader=TessellationReader(source,runtime)
        with self.assertRaises(ProgramError) as caught:reader.call('GetVertexPoint',123)
        error=caught.exception
        self.assertEqual(error.code,'RPC_CONNECTION_LOST')
        self.assertEqual(error.details['com_hresult'],-2147023174)
        self.assertEqual(error.details['com_method'],'GetVertexPoint')
        self.assertEqual(error.details['reader']['progress']['completed'],250)
        self.assertIsNone(runtime.reader_snapshot()['current_com_call'])
        self.assertEqual(runtime.reader_snapshot()['last_com_call']['com_hresult'],-2147023174)

    def test_bound_and_compat_read_the_same_arrays_and_count_hits(self):
        class Source:
            def GetVertexNormal(self,index):return (1.,0.,index)
        outputs=[]
        for backend in ('compat','bound'):
            with patch.dict(os.environ,{'SOLIDWORKS_MCP_TESSELLATION_READER':backend}):
                reader=TessellationReader(Source(),RuntimeState())
                outputs.append(reader.call('GetVertexNormal',42,array=True))
                reader.hit('GetVertexNormal')
                stats=reader.snapshot()['methods']['GetVertexNormal']
                self.assertEqual(stats['calls'],1)
                self.assertEqual(stats['cache_hits'],1)
                self.assertGreaterEqual(stats['conversion_sec'],0)
        self.assertEqual(outputs[0],outputs[1])

    def test_scope_restore_error_does_not_mask_original_rpc(self):
        class App:
            def __init__(self):self.value=False
            @property
            def CommandInProgress(self):return self.value
            @CommandInProgress.setter
            def CommandInProgress(self,value):
                if not value:raise RpcError('restore RPC')
                self.value=value
        with patch('solidworks_mcp.automation.com_utils.typed',side_effect=lambda value,_:value):
            with self.assertRaisesRegex(RpcError,'original RPC'):
                with command_scope(App()):raise RpcError('original RPC')


if __name__=='__main__':unittest.main()
