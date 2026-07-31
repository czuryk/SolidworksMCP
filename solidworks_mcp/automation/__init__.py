"""
SolidWorks Automation Package
-----------------------------
Complete automation class combining all operations.
"""

from .base import SolidWorksAutomation as _BaseAutomation
from .documents import DocumentOperations
from .sketches import SketchOperations
from .features import FeatureOperations
from .advanced_features import AdvancedFeatureOperations
from .more_features import MoreFeatureOperations
from .bodies import BodyOperations
from .geometry_probe import GeometryProbeOperations
from .view import ViewOperations
from .transactions import TransactionOperations
from .parametric import ParametricSketchOperations
from .vectorization import ImageSketchOperations
from .high_level import HighLevelOperations


# MoreFeatureOperations precedes FeatureOperations so its improved
# fillet_edges/chamfer_edges (ray edge selection) win over the legacy ones.
class SolidWorksAutomation(_BaseAutomation, DocumentOperations,
                           TransactionOperations, ParametricSketchOperations,
                           ImageSketchOperations, HighLevelOperations,
                           SketchOperations, MoreFeatureOperations,
                           FeatureOperations, AdvancedFeatureOperations,
                           BodyOperations, GeometryProbeOperations,
                           ViewOperations):
    """
    Complete SolidWorks automation class

    Combines all operation mixins:
    - Base: Connection, document access, freeze-bar protection, utilities
    - Documents: Create, open, save, close documents
    - Sketches: Create sketches, draw 2D geometry
    - Features: Extrude, cut, fillet, chamfer, list
    - AdvancedFeatures: delete/rename/status, advanced_extrude/advanced_cut
    - Bodies: list/show/hide/rename/transparency
    - GeometryProbe: probe_ray(s), select_face_by_ray, sketch_contour
    - View: take_screenshot, set_view_orientation

    Example:
        sw = SolidWorksAutomation()
        sw.connect()
        sw.create_new_part()
        sw.create_sketch("Front")
        sw.draw_circle(0, 0, 25)
        sw.extrude_sketch(10)
        sw.save_document("C:/Parts/MyPart.sldprt")
    """
    pass


__all__ = [
    "SolidWorksAutomation",
    "DocumentOperations",
    "SketchOperations",
    "FeatureOperations",
    "AdvancedFeatureOperations",
    "MoreFeatureOperations",
    "BodyOperations",
    "GeometryProbeOperations",
    "ViewOperations",
    "TransactionOperations",
    "ParametricSketchOperations",
    "ImageSketchOperations",
    "HighLevelOperations",
]
