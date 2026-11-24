"""HBB / OBB / polygon 的统一模型与格式转换（不依赖界面）。"""
from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path


class AnnotationError(ValueError):
    pass


@dataclass
class LabelShape:
    class_id: int
    class_name: str
    kind: str
    points: list[tuple[float, float]]
    confidence: float | None = None
    metadata: dict = field(default_factory=dict)


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def class_index(classes: list[str], name: str) -> int:
    if name not in classes:
        classes.append(name)
    return classes.index(name)


def bounds(points):
    if not points:
        raise AnnotationError("标注缺少坐标")
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def rectangle(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def polygon_area(points):
    return abs(sum(x * points[(i + 1) % len(points)][1] - y * points[(i + 1) % len(points)][0]
                   for i, (x, y) in enumerate(points))) / 2


def convert_shape(shape: LabelShape, kind: str, size: tuple[int, int]) -> LabelShape:
    result = copy.deepcopy(shape)
    if kind == shape.kind:
        return result
    if shape.kind not in {"detect", "obb", "segment"}:
        raise AnnotationError(f"暂不能将 {shape.kind} 转换为 {kind}")
    if kind == "detect":
        result.points = rectangle(*bounds(shape.points))
    elif kind == "obb":
        import cv2
        import numpy as np
        w, h = size
        pts = np.asarray([(x * w, y * h) for x, y in shape.points], dtype=np.float32)
        result.points = [(float(x) / w, float(y) / h) for x, y in cv2.boxPoints(cv2.minAreaRect(pts))]
    elif kind != "segment":
        raise AnnotationError(f"未知的目标类型：{kind}")
    result.kind = kind
    result.metadata.pop("coco_rle", None)
    return result


def read_labelme(path: Path, image_size: tuple[int, int], classes: list[str]):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data.get("shapes"), list):
        raise AnnotationError(f"{path.name} 不是 LabelMe / X-AnyLabeling 标注")
    width = float(data.get("imageWidth") or image_size[0])
    height = float(data.get("imageHeight") or image_size[1])
    width = width if width > 0 else image_size[0]
    height = height if height > 0 else image_size[1]
    if width <= 0 or height <= 0:
        raise AnnotationError(f"{path.name} 缺少有效图片尺寸")
    shapes = []
    for raw in data["shapes"]:
        kind = {"rectangle": "detect", "rotation": "obb", "polygon": "segment"}.get(raw.get("shape_type"))
        if kind is None:
            # 保留不支持的点、线等形状，编辑保存时不会丢失。
            kind = raw.get("shape_type", "unknown")
        points = [(float(p[0]), float(p[1])) for p in raw.get("points", [])]
        if data.get("coordinateSystem") != "normalized":
            points = [(x / width, y / height) for x, y in points]
        if any(not math.isfinite(v) for p in points for v in p):
            raise AnnotationError(f"{path.name} 含无效坐标")
        if kind == "detect" and len(points) >= 2:
            points = rectangle(*bounds(points))
        minimum = 4 if kind in {"detect", "obb"} else 3 if kind == "segment" else 1
        if len(points) < minimum:
            raise AnnotationError(f"{path.name} 中 {raw.get('label', '')} 的顶点数量不足")
        name = str(raw.get("label", "unknown"))
        confidence = float(raw["score"]) if raw.get("score") is not None else None
        shapes.append(LabelShape(class_index(classes, name), name, kind, points, confidence, copy.deepcopy(raw)))
    return shapes, data


def labelme_document(image_path: str, size: tuple[int, int], shapes: list[LabelShape], target: Path, original=None):
    data = copy.deepcopy(original or {})
    w, h = size
    try:
        image_reference = os.path.relpath(image_path, target.parent)
    except ValueError:  # Windows 图片与标注可位于不同盘符。
        image_reference = str(Path(image_path).resolve())
    data.update(version="5.5.0", imageWidth=w, imageHeight=h, imageData=None,
                imagePath=image_reference.replace("\\", "/"))
    data.pop("coordinateSystem", None)
    data.setdefault("flags", {})
    data["shapes"] = []
    for shape in shapes:
        if shape.metadata.get("rle_has_holes"):
            raise AnnotationError("含孔洞的 COCO RLE 无法无损导出为 LabelMe 多边形，请保留 COCO 格式")
        raw = {k: copy.deepcopy(v) for k, v in shape.metadata.items() if not k.startswith("coco_") and not k.startswith("rle_") and k != "original_points"}
        pts = [(x * w, y * h) for x, y in shape.points]
        kind = {"detect": "rectangle", "obb": "rotation", "segment": "polygon"}.get(shape.kind, shape.kind)
        if shape.kind == "detect":
            x1, y1, x2, y2 = bounds(pts)
            pts = [(x1, y1), (x2, y2)]
        raw.update(label=shape.class_name, points=pts, shape_type=kind, score=shape.confidence)
        raw.setdefault("group_id", None)
        raw.setdefault("flags", {})
        if kind == "rotation":
            raw["direction"] = math.atan2((shape.points[1][1] - shape.points[0][1]) * h,
                                          (shape.points[1][0] - shape.points[0][0]) * w) % (2 * math.pi)
        data["shapes"].append(raw)
    return data


def decode_rle(rle):
    """COCO counts：支持未压缩 runs 和 signed 5-bit delta 编码。"""
    import numpy as np
    h, w = map(int, rle["size"])
    if min(h, w) <= 0 or h * w > 100_000_000:
        raise AnnotationError("RLE 图片尺寸无效或过大")
    runs = rle.get("counts", [])
    if isinstance(runs, str):
        decoded, pos = [], 0
        while pos < len(runs):
            value, shift = 0, 0
            while True:
                if pos >= len(runs) or shift > 60:
                    raise AnnotationError("RLE 压缩数据无效")
                c = ord(runs[pos]) - 48
                if not 0 <= c <= 63:
                    raise AnnotationError("RLE 含无效字符")
                pos += 1
                value |= (c & 31) << shift
                shift += 5
                if not c & 32:
                    if c & 16:
                        value -= 1 << shift
                    break
            if len(decoded) > 2:
                value += decoded[-2]
            decoded.append(value)
        runs = decoded
    if not isinstance(runs, list) or any(not isinstance(x, int) or x < 0 for x in runs) or sum(runs) != h * w:
        raise AnnotationError("RLE 长度与图片尺寸不一致")
    flat = np.zeros(h * w, dtype=np.uint8)
    offset = 0
    for i, count in enumerate(runs):
        if i % 2:
            flat[offset:offset + count] = 1
        offset += count
    return flat.reshape((h, w), order="F")


def coco_shapes(data, classes):
    import cv2
    categories = {c["id"]: str(c["name"]) for c in data.get("categories", [])}
    for name in categories.values():
        class_index(classes, name)
    images = {i["id"]: i for i in data.get("images", [])}
    by_name = {str(i["file_name"]).replace("\\", "/"): [] for i in images.values()}
    for raw in data.get("annotations", []):
        image = images.get(raw.get("image_id"))
        if image is None or raw.get("category_id") not in categories:
            raise AnnotationError("COCO 标注引用了不存在的图片或类别")
        w, h = float(image.get("width", 0)), float(image.get("height", 0))
        if w <= 0 or h <= 0:
            raise AnnotationError(f"{image['file_name']} 缺少有效尺寸")
        name = categories[raw["category_id"]]
        metadata = {"coco_annotation": copy.deepcopy(raw), "group_id": raw.get("id")}
        segmentation = raw.get("segmentation")
        polygons = []
        kind = "segment"
        if isinstance(segmentation, dict):
            mask = decode_rle(segmentation)
            contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            metadata["coco_rle"] = copy.deepcopy(segmentation)
            metadata["rle_has_holes"] = bool(hierarchy is not None and any(item[3] >= 0 for item in hierarchy[0]))
            for i, contour in enumerate(contours):
                if hierarchy is not None and hierarchy[0][i][3] < 0:
                    points = contour.reshape(-1, 2).tolist()
                    if len(points) < 3:
                        x, y, cw, ch = cv2.boundingRect(contour)
                        points = rectangle(x, y, x + cw, y + ch)
                    polygons.append(points)
        elif isinstance(segmentation, list) and segmentation:
            if any(not isinstance(p, list) or len(p) < 6 or len(p) % 2 for p in segmentation):
                raise AnnotationError(f"{image['file_name']} 包含无效的 COCO polygon")
            polygons = [[(float(p[i]), float(p[i + 1])) for i in range(0, len(p), 2)] for p in segmentation if len(p) >= 6 and len(p) % 2 == 0]
            if raw.get("shape_type") == "rotation":
                kind = "obb"
        elif raw.get("bbox") and len(raw["bbox"]) == 4:
            x, y, bw, bh = map(float, raw["bbox"])
            polygons = [rectangle(x, y, x + bw, y + bh)]
            kind = "detect"
        elif raw.get("keypoints"):
            raise AnnotationError("COCO 关键点任务请先转换为框或多边形；当前编辑器支持 HBB / OBB / 分割")
        for points in polygons:
            normalized = [(x / w, y / h) for x, y in points]
            if any(not math.isfinite(v) for point in normalized for v in point):
                raise AnnotationError(f"{image['file_name']} 包含无效坐标")
            meta = copy.deepcopy(metadata)
            meta["original_points"] = copy.deepcopy(normalized)
            confidence = float(raw["score"]) if raw.get("score") is not None else None
            by_name[str(image["file_name"]).replace("\\", "/")].append(LabelShape(class_index(classes, name), name, kind, normalized, confidence, meta))
    return by_name


def shapes_to_coco(shapes, size, image_id, category_ids, first_id=1):
    w, h = size
    grouped = {}
    for index, shape in enumerate(shapes):
        if shape.kind not in {"detect", "obb", "segment"}:
            raise AnnotationError(f"COCO 导出暂不支持 {shape.kind}")
        original = shape.metadata.get("coco_annotation", {})
        group = original.get("id", shape.metadata.get("group_id"))
        key = (shape.class_name, shape.kind, group if group is not None else f"new_{index}")
        grouped.setdefault(key, []).append(shape)
    result = []
    for components in grouped.values():
        shape = components[0]
        ann = copy.deepcopy(shape.metadata.get("coco_annotation", {}))
        pixels = [[(x * w, y * h) for x, y in s.points] for s in components]
        x1, y1, x2, y2 = bounds([p for part in pixels for p in part])
        ann.update(id=first_id + len(result), image_id=image_id, category_id=category_ids[shape.class_name],
                   bbox=[x1, y1, x2 - x1, y2 - y1], iscrowd=ann.get("iscrowd", 0))
        unchanged_rle = shape.metadata.get("coco_rle") and all(s.points == s.metadata.get("original_points") for s in components)
        if unchanged_rle:
            ann["segmentation"] = copy.deepcopy(shape.metadata["coco_rle"])
            ann["area"] = int(decode_rle(ann["segmentation"]).sum())
            original_bbox = shape.metadata.get("coco_annotation", {}).get("bbox")
            if original_bbox:
                ann["bbox"] = copy.deepcopy(original_bbox)
        elif shape.kind != "detect":
            ann["segmentation"] = [[v for p in part for v in p] for part in pixels]
            ann["area"] = sum(polygon_area(part) for part in pixels)
        else:
            ann.pop("segmentation", None)
            ann["area"] = (x2 - x1) * (y2 - y1)
        if shape.confidence is not None:
            ann["score"] = shape.confidence
        if shape.kind == "obb":
            ann["shape_type"] = "rotation"  # 标准 segmentation 四角 + 可选类型信息。
        else:
            ann.pop("shape_type", None)
        result.append(ann)
    return result


def update_coco_image(data, file_name, shapes, size, classes):
    result = copy.deepcopy(data)
    image = next((i for i in result.get("images", []) if i["file_name"].replace("\\", "/") == file_name), None)
    if image is None:
        raise AnnotationError("COCO 中找不到当前图片")
    category_ids = {str(c["name"]): c["id"] for c in result.get("categories", [])}
    for name in classes:
        if name not in category_ids:
            cid = max(category_ids.values(), default=0) + 1
            result.setdefault("categories", []).append({"id": cid, "name": name, "supercategory": ""})
            category_ids[name] = cid
    others = [a for a in result.get("annotations", []) if a["image_id"] != image["id"]]
    first_id = max((a["id"] for a in others), default=0) + 1
    result["annotations"] = others + shapes_to_coco(shapes, size, image["id"], category_ids, first_id)
    return result


def export_dataset(records, classes, output: Path, target_format: str):
    """转换到一个新目录；先验证所有目标文件，禁止覆盖已有标注。"""
    names = list(classes)
    for record in records:
        for shape in record["shapes"]:
            class_index(names, shape.class_name)
    category_ids = {name: i + 1 for i, name in enumerate(names)}
    documents, text_files, seen = {}, {}, set()
    coco = {"info": {"description": "Image Browser annotation conversion"}, "images": [], "annotations": [],
            "categories": [{"id": cid, "name": name, "supercategory": ""} for name, cid in category_ids.items()]}
    warnings, shape_count = set(), 0
    for index, record in enumerate(records, 1):
        relative = Path(record["file_name"])
        if relative.is_absolute() or ".." in relative.parts:
            raise AnnotationError("图片相对路径超出数据集目录")
        size = record["size"]
        shapes = copy.deepcopy(record["shapes"])
        for shape in shapes:
            shape.class_id = names.index(shape.class_name)
            if any(not math.isfinite(v) for point in shape.points for v in point):
                raise AnnotationError(f"{relative} 含无效坐标")
        shape_count += len(shapes)
        if target_format == "coco":
            coco["images"].append({"id": index, "file_name": relative.as_posix(), "width": size[0], "height": size[1]})
            coco["annotations"] += shapes_to_coco(shapes, size, index, category_ids, len(coco["annotations"]) + 1)
            if any(s.kind == "obb" for s in shapes):
                warnings.add("COCO 以四角 segmentation 保存 OBB，并附加 shape_type=rotation；其他工具可能按多边形读取。")
            continue
        suffix = ".json" if target_format == "labelme" else ".txt"
        target = output / relative.with_suffix(suffix)
        if target in seen:
            raise AnnotationError(f"同名不同扩展名的图片会生成同一标签：{relative}，请先重命名")
        seen.add(target)
        if target_format == "labelme":
            documents[target] = labelme_document(record["image_path"], size, shapes, target, record.get("labelme_document"))
        else:
            kind = {"yolo_hbb": "detect", "yolo_obb": "obb", "yolo_segment": "segment"}.get(target_format)
            if kind is None:
                raise AnnotationError("请选择有效的导出格式")
            lines = []
            for shape in shapes:
                if shape.metadata.get("rle_has_holes") and kind == "segment":
                    raise AnnotationError("含孔洞 RLE 无法无损转为 YOLO polygon，请导出 COCO")
                if shape.kind != kind:
                    warnings.add("目标任务与原形状不同时，将按外接框 / 最小旋转外接框 / 顶点多边形转换，几何细节可能减少。")
                converted = convert_shape(shape, kind, size)
                if any(v < -1e-7 or v > 1 + 1e-7 for point in converted.points for v in point):
                    raise AnnotationError(f"{relative} 含超出图像的坐标，请调整后再导出 YOLO")
                if kind == "detect":
                    x1, y1, x2, y2 = bounds(converted.points)
                    values = [(x1+x2)/2, (y1+y2)/2, x2-x1, y2-y1]
                else:
                    values = [v for point in converted.points for v in point]
                lines.append(f"{converted.class_id} " + " ".join(f"{v:.8f}" for v in values))
                if shape.confidence is not None or shape.metadata:
                    warnings.add("标准 YOLO 训练标签不携带 score、flags、group_id 等附加字段；这些字段请通过 JSON 格式保留。")
            text_files[target] = "\n".join(lines) + ("\n" if lines else "")
    if target_format == "coco":
        documents[output / "annotations.json"] = coco
    if target_format.startswith("yolo_"):
        text_files[output / "classes.txt"] = "\n".join(names) + "\n"
        documents[output / ".annotation_task.json"] = {"task": {"yolo_hbb": "detect", "yolo_obb": "obb", "yolo_segment": "segment"}[target_format]}
    report = {"format": target_format, "images": len(records), "shapes": shape_count, "classes": names,
              "category_mapping": {name: {"yolo_id": i, "coco_id": i+1} for i, name in enumerate(names)},
              "warnings": sorted(warnings), "files": [record["file_name"] for record in records]}
    documents[output / "conversion_report.json"] = report
    for path in list(documents) + list(text_files):
        if path.exists():
            raise AnnotationError(f"目标文件已经存在，未覆盖：{path}")
    for path, content in documents.items():
        atomic_json(path, content)
    for path, content in text_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return report
