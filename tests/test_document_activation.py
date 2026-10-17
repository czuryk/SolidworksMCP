"""Opening an already loaded part must activate and verify the exact target."""
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from solidworks_mcp.automation import SolidWorksAutomation

class DocumentActivationTests(unittest.TestCase):
    def test_loaded_document_is_activated(self):self.check('switch')
    def test_wrong_active_document_cannot_report_success(self):self.check('wrong')
    def test_current_document_does_not_need_activation(self):self.check('current')

    def check(self,mode):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'requested.SLDPRT';path.write_bytes(b'fixture')
            wanted=SimpleNamespace(path=str(path));other=SimpleNamespace(path=str(Path(directory)/'other.SLDPRT'))
            sw=SolidWorksAutomation();sw._connected=True
            app=SimpleNamespace(RevisionNumber='34.2.1',ActiveDoc=wanted if mode=='current' else other,
                                OpenDoc6=Mock(return_value=wanted))
            def activate(*args):
                if mode=='switch':app.ActiveDoc=wanted
                return wanted,0
            app.ActivateDoc3=Mock(side_effect=activate);sw._sw_app=app
            with patch.object(sw,'_get_doc_path',side_effect=lambda d:d.path), \
                    patch.object(sw,'_get_doc_title',return_value=path.name), \
                    patch('solidworks_mcp.automation.documents.typed',side_effect=lambda obj,name:obj), \
                    patch('solidworks_mcp.automation.documents.resolve_solidworks_constant',return_value=1):
                result=sw.open_document(str(path))
            self.assertEqual(result['success'],mode!='wrong',result)
            if mode=='wrong':self.assertEqual(result['data']['error']['code'],'DOCUMENT_ACTIVATION_FAILED')
            else:self.assertTrue(result['data']['active_document_verified'])
            self.assertEqual(app.ActivateDoc3.call_count,0 if mode=='current' else 1)

if __name__=='__main__':unittest.main()
