import unittest
from PIL import Image
from src.annotation_formats import LabelShape, rectangle
from src.review_support import compare_shapes, overlap, viewport_spec, render_viewport


def shape(name="crack", points=None, kind="detect"):
    return LabelShape(0,name,kind,points or rectangle(.1,.1,.5,.5))


class ReviewGeometryTests(unittest.TestCase):
    def test_identical_and_different_classes(self):
        a,b=shape(),shape()
        abnormal,pairs=compare_shapes([a],[b])
        self.assertFalse(abnormal)
        self.assertIs(pairs[id(a)],b)
        b.class_name="water"
        self.assertEqual(compare_shapes([a],[b])[0],{id(a),id(b)})

    def test_unmatched_and_displaced_are_red(self):
        a,b=shape(),shape(points=rectangle(.4,.1,.8,.5))
        self.assertEqual(compare_shapes([a],[b])[0],{id(a),id(b)})
        self.assertEqual(compare_shapes([a],[])[0],{id(a)})

    def test_matching_is_one_to_one(self):
        a,b,c=shape(),shape(),shape()
        abnormal,_=compare_shapes([a],[b,c])
        self.assertEqual(len(abnormal),1)

    def test_concave_shapes_do_not_match_only_by_bounds(self):
        a=shape(kind="segment",points=[(0,0),(1,0),(1,.1),(.1,.1),(.1,1),(0,1)])
        b=shape(kind="segment",points=[(0,.9),(.9,.9),(.9,0),(1,0),(1,1),(0,1)])
        self.assertLess(overlap(a,b),.2)
        self.assertEqual(len(compare_shapes([a],[b])[0]),2)

    def test_eight_k_at_eight_x_is_viewport_bounded(self):
        image=Image.new("RGB",(8192,4000),"white")
        spec=viewport_spec(image.size,image.size,8,(-12000,-6000),(1000,700))
        output=render_viewport(image,spec)
        self.assertEqual(output.size,(1000,700))
        self.assertEqual(output.getpixel((500,350)),(255,255,255))
        output.close();image.close()

    def test_offscreen_does_not_allocate_output(self):
        self.assertIsNone(viewport_spec((800,400),(800,400),2,(1000,0),(900,600)))


if __name__=="__main__": unittest.main()
