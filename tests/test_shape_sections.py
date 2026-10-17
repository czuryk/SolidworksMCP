"""Native ray sampling must expose missing/ambiguous evidence and flat sections."""
import math
import unittest
from solidworks_mcp.automation.shape_sections import validate_sections,section_directions,radial_results
from solidworks_mcp.shape_program import ProgramError


class SectionTests(unittest.TestCase):
    def test_frame_normal_and_unit_rays(self):
        normal=[1,2,3]
        for direction in section_directions(normal,16):
            self.assertAlmostEqual(math.hypot(*direction),1)
            self.assertAlmostEqual(sum(a*b for a,b in zip(normal,direction)),0)

    def test_circle_and_flat_native_hits_are_distinct(self):
        directions=section_directions([0,1,0],16)
        for flat in (False,True):
            points=[]
            for i,d in enumerate(directions):
                radius=min(5/max(abs(d[0]),1e-12),1/max(abs(d[2]),1e-12)) if flat else 5
                points.extend([0,i,33,*[r*radius/1000 for r in d],0,0,0])
            r=radial_results(points,16,directions,[0,0,0],32,16)
            self.assertEqual(r['status'],'SAMPLED')
            self.assertEqual(r['radius_min_mm'],1 if flat else 5)
            self.assertEqual(r['radius_max_mm'],5)

    def test_seam_duplicates_missing_and_outside_center(self):
        directions=[[1,0,0],[0,0,1]]
        result=radial_results([0,0,33,.005,0,0,0,0,0,0,0,36,.005,0,0,0,0,0],2,directions,[0,0,0],32,16)
        self.assertEqual(result['radii_mm'],[5,None])
        self.assertEqual(result['status'],'UNKNOWN')
        result=radial_results([0,0,17,.005,0,0,0,0,0],1,[[1,0,0]],[0,0,0],32,16)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertIsNone(result['radii_mm'][0])

    def test_invalid_or_unbounded_queries_rejected(self):
        q={'component':'Body','center':[0,0,0],'normal':[0,1,0]}
        for override in ({'normal':[0,0,0]},{'samples':1000},{'center':[float('nan'),0,0]},
                         {'expected_radius':5},{'center_typo':[0,0,0]}):
            with self.assertRaises(ProgramError):validate_sections([{**q,**override}])


if __name__=='__main__':unittest.main()
