#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SolidworksMCP - Entry Point
------------------------------------
Run this file to start the MCP server.

Version: 6.5.31
Author: Samsaam Ali Baig

Usage:
    python solidworks_mcp_server.py

Or configure in any MCP client:
    {
        "mcpServers": {
            "solidworks": {
                "command": "python",
                "args": ["path/to/solidworks_mcp_server.py"]
            }
        }
    }
"""

import sys
import os

# Add package to path
package_dir = os.path.dirname(os.path.abspath(__file__))
if package_dir not in sys.path:
    sys.path.insert(0, package_dir)

# Import and run server
from solidworks_mcp.server import run

if __name__ == "__main__":
    run()
