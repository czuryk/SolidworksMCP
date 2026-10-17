# Eiffel union and export regression

`source.py` is the original 128-node 150 mm tower; `manifest.json` is its
6.29.5 accepted delivery. Keep these files unchanged. Native control copy:
`D:\TMP\3dmodels\evidence\control_model.SLDPRT` (external binary fixture).

SHA-256: `eb6e6deeb16a39f3735b9b799ea448e79eff8fc4cfda1c43ef4cd7481627cbe0`.
Original revision: `48e0512862f0f1e5b5e562e420a9ae3da4314011f23a66f2262fbf8d5bbaaff3`.
1 body, 1,292 native faces, 186,152 STL triangles, no mesh repair.

`live_union_regression.py` derives platform-first, supports-first and reversed
lower-tower inputs from this exact source; it never overwrites the fixture.
`live_export_benchmark.py` verifies the control hash and works on a new copy.

The 6.30.0rc1 live results are pending. The previous successful export was about
229.7 seconds, with 185.0966 seconds of tessellation reads; this is historical
evidence, not a measurement of the new reader. The typed 2026 ITessellation
wrapper exposes per-facet/fin/vertex access and no bulk all-vertices accessor.
The unchanged access pattern is documented in the
[official tessellation example](https://help.solidworks.com/2024/English/api/sldworksapi/Tessellate_a_Body_Example_CSharp.htm?format=P&value=).
