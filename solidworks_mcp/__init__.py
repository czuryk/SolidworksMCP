"""
SolidworksMCP Package
----------------------
Model Context Protocol server for SolidWorks automation.

Version: 6.5.31
Author: Samsaam Ali Baig
"""

__version__ = "6.5.31"
__author__ = "Samsaam Ali Baig"

from .automation import SolidWorksAutomation
from .config import get_config, reload_config, save_config, SolidWorksConfig
from .constants import SwErrors, SwPlanes, SwDocumentTypes, SwViews
from .utils import (
    UnitConverter,
    mm, cm, inch, ft,
    find_solidworks,
    find_template,
    get_solidworks_info
)

__all__ = [
    # Version
    "__version__",
    "__author__",

    # Main class
    "SolidWorksAutomation",

    # Config
    "get_config",
    "reload_config",
    "save_config",
    "SolidWorksConfig",

    # Constants
    "SwErrors",
    "SwPlanes",
    "SwDocumentTypes",
    "SwViews",

    # Utils
    "UnitConverter",
    "mm", "cm", "inch", "ft",
    "find_solidworks",
    "find_template",
    "get_solidworks_info",
]
