import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from src.annotation_formats import (LabelShape, AnnotationError, atomic_json, read_labelme, labelme_document,
    coco_shapes, decode_rle, export_dataset, update_coco_image, convert_shape)
from src.tk_app import LabelStore


class AnnotationFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def labelme_fixture(self):
        return {"version": "5.5.0", "imageWidth": 200, "imageHeight": 100, "imagePath": "test.jpg",
                "flags": {"reviewed": True}, "description": "preserve", "shapes": [
                    {"label": "crack", "shape_type": "rectangle", "points": [[20, 10], [80, 50]], "group_id": 3, "flags": {"difficult": True}, "attributes": {"wet": False}},
                    {"label": "seepage", "shape_type": "polygon", "points": [[100, 10], [180, 15], [170, 80], [110, 70]], "group_id": 5},
                    {"label": "crack", "shape_type": "rotation", "points": [[40, 10], [80, 30], [60, 70], [20, 50]], "direction": .5}]}

    def coco_fixture(self):
        return {"images": [{"id": 11, "file_name": "a/test.jpg", "width": 200, "height": 100},
                           {"id": 22, "file_name": "b/test.jpg", "width": 200, "height": 100}],
                "categories": [{"id": 3, "name": "crack"}, {"id": 99, "name": "seepage"}],
                "annotations": [{"id": 7, "image_id": 11, "category_id": 99, "bbox": [10, 10, 40, 30],
                                 "segmentation": [[10, 10, 50, 10, 45, 40, 10, 30]], "iscrowd": 0, "custom": "preserve"},
                                {"id": 8, "image_id": 22, "category_id": 3, "bbox": [20, 10, 40, 30], "iscrowd": 0}]}

    def test_labelme_geometry_types_and_metadata_roundtrip(self):
        path = self.root / "test.json"
        atomic_json(path, self.labelme_fixture())
        classes = []
        shapes, original = read_labelme(path, (200, 100), classes)
        self.assertEqual([s.kind for s in shapes], ["detect", "segment", "obb"])
        self.assertEqual(shapes[0].points[0], (.1, .1))
        output = labelme_document(str(self.root / "test.jpg"), (200, 100), shapes, path, original)
        self.assertEqual(output["flags"], original["flags"])
        self.assertEqual(output["description"], "preserve")
        self.assertEqual(output["shapes"][0]["attributes"], {"wet": False})
        self.assertEqual(output["shapes"][0]["group_id"], 3)
        self.assertEqual(output["shapes"][1]["shape_type"], "polygon")
        atomic_json(path, output)
        reread, _ = read_labelme(path, (200, 100), [])
        for a, b in zip(shapes, reread):
            np.testing.assert_allclose(a.points, b.points)

    def test_tiny_labelme_pixel_coordinates_are_not_mistaken_for_normalized(self):
        data = self.labelme_fixture()
        data["shapes"] = [{"label": "tiny", "shape_type": "rectangle", "points": [[0, 0], [1, 1]]}]
        path = self.root / "tiny.json"
        atomic_json(path, data)
        shapes, _ = read_labelme(path, (200, 100), [])
        self.assertEqual(shapes[0].points[2], (.005, .01))

    def test_coco_segmentation_does_not_duplicate_bbox(self):
        shapes = coco_shapes(self.coco_fixture(), [])
        self.assertEqual(len(shapes["a/test.jpg"]), 1)
        self.assertEqual(shapes["a/test.jpg"][0].kind, "segment")

    def test_coco_edit_preserves_other_images_and_sparse_category_ids(self):
        source = self.coco_fixture()
        classes = []
        shapes = coco_shapes(source, classes)["a/test.jpg"]
        shapes[0].class_id = 0
        shapes[0].class_name = "crack"
        edited = update_coco_image(source, "a/test.jpg", shapes, (200, 100), classes)
        other = [a for a in edited["annotations"] if a["image_id"] == 22]
        self.assertEqual(other, [source["annotations"][1]])
        updated = next(a for a in edited["annotations"] if a["image_id"] == 11)
        self.assertEqual(updated["category_id"], 3)
        self.assertEqual(updated["custom"], "preserve")
        self.assertEqual(source, self.coco_fixture())

    def test_coco_multiple_parts_keep_one_instance(self):
        source = self.coco_fixture()
        source["annotations"][0]["segmentation"].append([100, 10, 120, 10, 120, 30, 100, 30])
        classes = []
        shapes = coco_shapes(source, classes)["a/test.jpg"]
        self.assertEqual(len(shapes), 2)
        output = update_coco_image(source, "a/test.jpg", shapes, (200, 100), classes)
        self.assertEqual(len([a for a in output["annotations"] if a["image_id"] == 11]), 1)
        self.assertEqual(len(output["annotations"][-1]["segmentation"]), 2)

    def test_rle_compressed_and_uncompressed(self):
        np.testing.assert_array_equal(decode_rle({"size": [2, 2], "counts": "04"}), np.ones((2, 2)))
        np.testing.assert_array_equal(decode_rle({"size": [3, 3], "counts": "052LO"}),
                                      decode_rle({"size": [3, 3], "counts": [0, 5, 2, 1, 1]}))
        with self.assertRaises(AnnotationError):
            decode_rle({"size": [3, 3], "counts": [1, 3]})

    def test_rle_holes_retained_in_coco_and_blocked_in_polygon_export(self):
        source = {"images": [{"id": 1, "file_name": "mask.jpg", "width": 3, "height": 3}],
                  "categories": [{"id": 1, "name": "mask"}], "annotations": [
                      {"id": 1, "image_id": 1, "category_id": 1, "bbox": [0, 0, 3, 3],
                       "segmentation": {"size": [3, 3], "counts": [0, 4, 1, 4]}, "iscrowd": 1}]}
        classes = []
        shapes = coco_shapes(source, classes)["mask.jpg"]
        self.assertTrue(shapes[0].metadata["rle_has_holes"])
        result = update_coco_image(source, "mask.jpg", shapes, (3, 3), classes)
        self.assertEqual(result["annotations"][0]["segmentation"], source["annotations"][0]["segmentation"])
        with self.assertRaises(AnnotationError):
            labelme_document("mask.jpg", (3, 3), shapes, self.root / "mask.json")

    def test_yolo_polygon_does_not_lose_vertices_and_hbb_uses_center(self):
        (self.root / "p.txt").write_text("0 .1 .1 .3 .1 .4 .4 .3 .7 .1 .6\n", encoding="utf-8")
        store = LabelStore()
        store.configure("yolo", "auto", str(self.root))
        shapes = store.load_for_image(str(self.root / "p.jpg"), (200, 100))
        self.assertEqual(shapes[0].kind, "segment")
        self.assertEqual(len(shapes[0].points), 5)
        (self.root / "h.txt").write_text("0 .2 .2 .4 .4\n", encoding="utf-8")
        store.clear_label_cache()
        shape = store.load_for_image(str(self.root / "h.jpg"), (200, 100))[0]
        self.assertEqual(shape.points[0], (0., 0.))
        self.assertEqual(shape.points[2], (.4, .4))

    def test_yolo_task_metadata_disambiguates_four_point_polygon(self):
        shape = LabelShape(0, "surface", "segment", [(.1,.1),(.8,.1),(.8,.8),(.1,.8)])
        record = {"image_path": str(self.root/"p.jpg"), "file_name": "sub/p.jpg", "size": (200,100), "shapes": [shape]}
        export_dataset([record], ["surface"], self.root/"out", "yolo_segment")
        store = LabelStore()
        store.image_root = str(self.root/"images")
        store.configure("auto", "auto", str(self.root/"out"))
        reread = store.load_for_image(str(self.root/"images"/"sub"/"p.jpg"), (200,100))
        self.assertEqual(reread[0].kind, "segment")

    def test_export_all_formats_keep_relative_paths_and_dimensions(self):
        data = self.labelme_fixture()
        path = self.root/"source.json"
        atomic_json(path, data)
        classes = []
        shapes, _ = read_labelme(path, (200,100), classes)
        records = [{"image_path": str(self.root/"images"/"a"/"p.jpg"), "file_name": "a/p.jpg", "size": (200,100), "shapes": shapes}]
        for fmt in ["labelme", "coco", "yolo_hbb", "yolo_obb", "yolo_segment"]:
            out = self.root/fmt
            report = export_dataset(records, classes, out, fmt)
            self.assertEqual(report["shapes"], 3)
            if fmt == "labelme":
                self.assertTrue((out/"a"/"p.json").exists())
            elif fmt == "coco":
                result=json.loads((out/"annotations.json").read_text())
                self.assertEqual(result["images"][0]["width"], 200)
                self.assertEqual(result["images"][0]["file_name"], "a/p.jpg")
                self.assertEqual(len(result["annotations"]), 3)
            else:
                self.assertEqual(len((out/"a"/"p.txt").read_text().splitlines()), 3)
        with self.assertRaises(AnnotationError):
            export_dataset(records, classes, self.root/"labelme", "labelme")

    def test_missing_geometry_or_unsupported_shape_does_not_silently_export(self):
        shape = LabelShape(0,"point","point",[(.1,.1)])
        records=[{"image_path":"p.jpg","file_name":"p.jpg","size":(200,100),"shapes":[shape]}]
        with self.assertRaises(AnnotationError):
            export_dataset(records,["point"],self.root/"out","yolo_hbb")
        self.assertFalse((self.root/"out").exists())

    def test_labelme_detection_ignores_classes_and_conversion_report(self):
        atomic_json(self.root/"p.json",self.labelme_fixture())
        atomic_json(self.root/"conversion_report.json",{"format":"labelme"})
        (self.root/"classes.txt").write_text("crack\n")
        store=LabelStore()
        store.configure("auto","auto",str(self.root))
        self.assertEqual(store.effective_format(),"labelme")


if __name__ == "__main__":
    unittest.main()
