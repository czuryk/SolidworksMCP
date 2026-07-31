"""
SolidworksMCP Utilities
------------------------
Utility modules for unit conversion, SolidWorks detection, and validation.
"""

from .units import (
    Unit,
    UnitConverter,
    mm, cm, inch, ft,
    to_mm, to_inch,
    get_converter,
    set_default_unit,
)

from .sw_finder import (
    SolidWorksFinder,
    find_solidworks,
    find_template,
    get_solidworks_info,
)

__all__ = [
    # Units
    "Unit",
    "UnitConverter",
    "mm", "cm", "inch", "ft",
    "to_mm", "to_inch",
    "get_converter",
    "set_default_unit",

    # SolidWorks Finder
    "SolidWorksFinder",
    "find_solidworks",
    "find_template",
    "get_solidworks_info",
]
