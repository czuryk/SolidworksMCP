"""Native reference results must survive the MCP JSON-RPC writer."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
from mcp.types import JSONRPCMessage, JSONRPCResponse

from solidworks_mcp.mesh_checks import MeshSilhouette
from solidworks_mcp.reference_compare import compare_reference
from solidworks_mcp.server import tool_result_content


class NativeComparisonTransportTests(unittest.TestCase):
    def test_native_result_round_trips_through_mcp_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reference.png"
            raster = np.full((300, 300), 255, dtype=np.uint8)
            raster[50:250, 100:200] = 0
            Image.fromarray(raster).convert("RGB").save(path)
            for radius in (10, 14):
                for height in (40, None):
                    with self.subTest(radius=radius, height=height):
                        triangles = np.array([
                            [[-radius, 0, 0], [radius, 0, 0], [radius, 40, 0]],
                            [[-radius, 0, 0], [radius, 40, 0], [-radius, 40, 0]],
                        ], dtype=float)
                        result = compare_reference(
                            str(path), MeshSilhouette(triangles), "front",
                            height_mm=height, directory=folder,
                        )
                        rendered = tool_result_content("compare_reference", {
                            "success": True, "message": "Native comparison",
                            "error_code": 0, "error_name": "swSuccess", "data": result,
                        })
                        # This is the serialization boundary used by stdout_writer.
                        envelope = JSONRPCMessage(JSONRPCResponse(
                            jsonrpc="2.0", id=7, result=rendered.model_dump(by_alias=True),
                        ))
                        wire = json.loads(envelope.model_dump_json(
                            by_alias=True, exclude_none=True,
                        ))
                        data = wire["result"]["structuredContent"]["data"]
                        self.assertIs(type(data["levels"]["passed"]), int)
                        self.assertIs(type(data["levels"]["failed"]), int)
                        self.assertEqual(data["levels"]["passed"], 24 if radius == 10 else 0)
                        self.assertEqual(data["levels"]["failed"], 0 if radius == 10 else 24)
                        for level in data["worst"]:
                            self.assertIs(level["passed"], False)
                        # Text and structured results must expose identical JSON types.
                        details = rendered.content[0].text.split("Details: ", 1)[1]
                        self.assertEqual(json.loads(details), data)


if __name__ == "__main__":
    unittest.main()

