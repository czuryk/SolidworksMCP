"""
SolidWorks Document Operations
------------------------------
Create, open, save, and manage SolidWorks documents.
"""

import os
import logging
import traceback
from typing import Optional, Dict

import win32com.client
import pythoncom

from ..constants import SwErrors, SwDocumentTypes, SwFileTypes, SwSaveAsOptions
from ..utils import find_template
from .com_utils import com_get, resolve_solidworks_constant, typed

logger = logging.getLogger(__name__)


class DocumentOperations:
    """
    Mixin class for document operations

    Requires parent class to have:
    - self._sw_app: SolidWorks application object
    - self.is_connected: Connection status property
    - self.connect(): Connection method
    - self._result(): Result factory method
    - self._units: UnitConverter instance
    """

    def _resolve_document_template(self, document_type):
        """Resolve configured, discovered, or SOLIDWORKS-default template."""
        kind = str(document_type).lower()
        attributes = {
            "part": "part_template",
            "assembly": "assembly_template",
            "drawing": "drawing_template",
        }
        preference_names = {
            "part": "swDefaultTemplatePart",
            "assembly": "swDefaultTemplateAssembly",
            "drawing": "swDefaultTemplateDrawing",
        }
        if kind not in attributes:
            return None, "unsupported"
        configured = str(getattr(
            self._config, attributes[kind], "auto") or "auto")
        if configured.lower() != "auto":
            path = os.path.abspath(configured)
            return (path, "mcp_config") if os.path.isfile(path) else (
                None, "configured_path_missing")
        discovered = find_template(kind)
        if discovered and os.path.isfile(discovered):
            return os.path.abspath(discovered), "filesystem_discovery"
        try:
            preference = resolve_solidworks_constant(preference_names[kind])
            native_default = str(com_get(
                self._sw_app, "GetUserPreferenceStringValue", preference,
                default="") or "")
            if native_default and os.path.isfile(native_default):
                return os.path.abspath(native_default), "solidworks_user_preference"
        except (LookupError, OSError):
            pass
        return None, "not_configured"

    def _create_from_template(self, document_type):
        template, source = self._resolve_document_template(document_type)
        if not template:
            return None, self._error(
                "CAPABILITY_UNAVAILABLE",
                f"No valid {document_type} template is configured",
                recoverable=True,
                recommended_actions=[
                    "Set the default template in SOLIDWORKS System Options, "
                    "or configure its absolute path in solidworks_mcp/config.json."],
                details={"document_type": document_type,
                         "template_source": source})
        doc = self._sw_app.NewDocument(template, 0, 0, 0)
        if doc is None:
            return None, self._error(
                "COM_MEMBER_MISMATCH",
                f"SOLIDWORKS rejected the {document_type} template",
                recoverable=True,
                details={"template": template, "template_source": source})
        return doc, {"template": template, "template_source": source}

    def create_new_part(self) -> Dict:
        """
        Create a new part document

        Returns:
            Result dictionary with document info
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            doc, template_info = self._create_from_template("part")
            if doc is None:
                return template_info

            # Set view
            try:
                doc.ShowNamedView2("*Isometric", 7)
                doc.ViewZoomtofit2()
            except:
                pass

            title = self._get_doc_title(doc)

            return self._result(True, f"Created part: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "type": "Part",
                               **template_info})

        except Exception as e:
            logger.error(f"Create part error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def create_new_assembly(self) -> Dict:
        """
        Create a new assembly document

        Returns:
            Result dictionary with document info
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            doc, template_info = self._create_from_template("assembly")
            if doc is None:
                return template_info

            try:
                doc.ShowNamedView2("*Isometric", 7)
                doc.ViewZoomtofit2()
            except:
                pass

            title = self._get_doc_title(doc)

            return self._result(True, f"Created assembly: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "type": "Assembly",
                               **template_info})

        except Exception as e:
            logger.error(f"Create assembly error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def create_new_drawing(self, paper_size: str = "A4") -> Dict:
        """
        Create a new drawing document

        Args:
            paper_size: Paper size (A4, A3, A2, A1, Letter)

        Returns:
            Result dictionary with document info
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            doc, template_info = self._create_from_template("drawing")
            if doc is None:
                return template_info

            title = self._get_doc_title(doc)

            return self._result(True, f"Created drawing: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "type": "Drawing",
                               "paper_size": paper_size, **template_info})

        except Exception as e:
            logger.error(f"Create drawing error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def open_document(self, filepath: str) -> Dict:
        """
        Open an existing document

        Args:
            filepath: Path to SolidWorks file

        Returns:
            Result dictionary
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            if not os.path.exists(filepath):
                return self._result(False, f"File not found: {filepath}",
                                  SwErrors.swFileNotFoundError)

            # Determine document type from extension
            ext = os.path.splitext(filepath)[1].lower()
            type_map = {
                ".sldprt": SwDocumentTypes.swDocPART,
                ".sldasm": SwDocumentTypes.swDocASSEMBLY,
                ".slddrw": SwDocumentTypes.swDocDRAWING,
            }
            doc_type = type_map.get(ext, SwDocumentTypes.swDocPART)

            # Open document
            errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

            doc = self._sw_app.OpenDoc6(filepath, int(doc_type), 0, "", errors, warnings)

            if doc is None or errors.value != 0:
                return self._result(False, f"Failed to open (error {errors.value})",
                                  SwErrors.swFileLoadError)

            title = self._get_doc_title(doc)

            return self._result(True, f"Opened: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "path": filepath})

        except Exception as e:
            logger.error(f"Open document error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def save_document(self, filepath: str = None) -> Dict:
        """
        Save the active document
        FIXED v4.1: Use doc.SaveAs() as primary method (avoids COM type mismatch
        with Extension.SaveAs where None parameter fails in SW 2025).

        Args:
            filepath: Path to save (None = save in place)

        Returns:
            Result dictionary
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            if filepath:
                # Ensure absolute path
                filepath = os.path.abspath(filepath)

                # Ensure directory exists
                dir_path = os.path.dirname(filepath)
                if dir_path and not os.path.exists(dir_path):
                    os.makedirs(dir_path)

                saved = False
                method_used = ""

                # Method 1: doc.SaveAs (simplest, most reliable for SW 2025)
                try:
                    result = doc.SaveAs(filepath)
                    if result:
                        saved = True
                        method_used = "SaveAs"
                except Exception as e:
                    logger.debug(f"doc.SaveAs failed: {e}")

                # Method 2: Extension.SaveAs with proper VARIANT null dispatch
                if not saved:
                    try:
                        empty_export = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
                        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                        result = doc.Extension.SaveAs(
                            filepath, 0, 0, empty_export, errors, warnings
                        )
                        if result and errors.value == 0:
                            saved = True
                            method_used = "Extension.SaveAs"
                    except Exception as e:
                        logger.debug(f"Extension.SaveAs failed: {e}")

                # Method 3: Extension.SaveAs2
                if not saved:
                    try:
                        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                        result = doc.Extension.SaveAs2(
                            filepath, 0, 0, None, "", False, errors, warnings
                        )
                        if result:
                            saved = True
                            method_used = "Extension.SaveAs2"
                    except Exception as e:
                        logger.debug(f"Extension.SaveAs2 failed: {e}")

                if not saved:
                    return self._result(False, "Save failed - all methods attempted",
                                      SwErrors.swFileSaveError)

                return self._result(True, f"Saved: {filepath} [{method_used}]",
                                  SwErrors.swSuccess,
                                  {"path": filepath, "method": method_used})
            else:
                # Save in place. Save3 has [in,out] errors/warnings params:
                # the typed wrapper requires explicit placeholders and
                # returns a tuple (ok, errors, warnings); calling the
                # dynamic version without them raises Type mismatch.
                ok = False
                errors_v = 0
                warnings_v = 0
                saved_via = ""

                md_typed = typed(doc, "IModelDoc2")
                if md_typed is not None:
                    try:
                        ret = md_typed.Save3(
                            int(SwSaveAsOptions.swSaveAsOptions_Silent), 0, 0)
                        if isinstance(ret, tuple):
                            ok = bool(ret[0])
                            errors_v = ret[1] if len(ret) > 1 else 0
                            warnings_v = ret[2] if len(ret) > 2 else 0
                        else:
                            ok = bool(ret)
                        saved_via = "IModelDoc2.Save3(typed)"
                    except Exception as e:
                        logger.debug(f"Typed Save3 failed: {e}")

                if not ok and not saved_via:
                    # Dynamic fallback with VARIANT byref out-params
                    try:
                        errors = win32com.client.VARIANT(
                            pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        warnings = win32com.client.VARIANT(
                            pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        ok = bool(doc.Save3(
                            int(SwSaveAsOptions.swSaveAsOptions_Silent),
                            errors, warnings))
                        errors_v = errors.value
                        warnings_v = warnings.value
                        saved_via = "Save3(dynamic)"
                    except Exception as e:
                        logger.debug(f"Dynamic Save3 failed: {e}")

                if not ok:
                    return self._result(False,
                        f"Save failed (errors={errors_v}, warnings={warnings_v})",
                        SwErrors.swFileSaveError,
                        {"errors": errors_v, "warnings": warnings_v})

                path = self._get_doc_path(doc)
                return self._result(True, f"Saved: {path}",
                                  SwErrors.swSuccess,
                                  {"path": path, "method": saved_via,
                                   "errors": errors_v, "warnings": warnings_v})

        except Exception as e:
            logger.error(f"Save error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileSaveError)

    def close_document(self, save: bool = False) -> Dict:
        """
        Close the active document

        Args:
            save: Save before closing

        Returns:
            Result dictionary
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return self._result(True, "No document to close")

            title = self._get_doc_title(doc)

            if save:
                # Reuse save_document: raw doc.Save3(0,0,0) mishandles the
                # [in,out] errors/warnings params (Type mismatch on SW2026)
                self.save_document()

            self._sw_app.CloseDoc(title)

            return self._result(True, f"Closed: {title}",
                              SwErrors.swSuccess, {"document": title})

        except Exception as e:
            logger.error(f"Close error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def get_document_info(self) -> Dict:
        """
        Get information about the active document.
        FIXED v5.0: GetType is a PROPERTY (int) in SW 2025/2026 dynamic COM -
        calling it as doc.GetType() raised "'int' object is not callable".
        All member access now goes through com_get().

        Returns:
            Result dictionary with document details
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            type_names = {
                0: "None",
                1: "Part",
                2: "Assembly",
                3: "Drawing"
            }

            doc_type = com_get(doc, "GetType", default=-1)
            title = self._get_doc_title(doc)
            path = self._get_doc_path(doc)

            info = {
                "title": title,
                "path": path if path else "Not saved",
                "type": type_names.get(doc_type, "Unknown"),
                "type_code": doc_type,
            }

            # Extra diagnostics (best effort)
            try:
                info["feature_count"] = com_get(
                    doc, "GetFeatureCount", default=None)
            except Exception:
                pass
            try:
                active_sketch = doc.SketchManager.ActiveSketch
                info["has_active_sketch"] = active_sketch is not None
            except Exception:
                info["has_active_sketch"] = None
            if doc_type == int(SwDocumentTypes.swDocPART):
                try:
                    bodies = doc.GetBodies2(0, False)  # solid bodies incl. hidden
                    info["solid_body_count"] = len(bodies) if bodies else 0
                except Exception:
                    info["solid_body_count"] = None

            return self._result(True, f"{title} ({info['type']})",
                              SwErrors.swSuccess, info)

        except Exception as e:
            logger.error(f"Get info error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def list_open_documents(self) -> Dict:
        """
        List all open documents

        Returns:
            Result dictionary with document list
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            docs = []

            # GetFirstDocument is a PROPERTY in SW 2025 COM, not a method.
            # Calling it as a method raises DISP_E_MEMBERNOTFOUND
            # (-2147352573, "Member not found").
            try:
                doc = self._sw_app.GetFirstDocument
            except AttributeError:
                doc = self._sw_app.GetFirstDocument()

            while doc:
                try:
                    # GetTitle / GetType are properties in SW 2025
                    title = doc.GetTitle
                    try:
                        doc_type = doc.GetType
                    except:
                        doc_type = doc.GetType()

                    type_names = {1: "Part", 2: "Assembly", 3: "Drawing"}

                    docs.append({
                        "title": title,
                        "type": type_names.get(doc_type, "Unknown")
                    })
                except:
                    pass

                # GetNext is a PROPERTY in SW 2025 COM
                try:
                    doc = doc.GetNext
                except:
                    break

            return self._result(True, f"{len(docs)} document(s) open",
                              SwErrors.swSuccess, {"documents": docs})

        except Exception as e:
            logger.error(f"List documents error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)
