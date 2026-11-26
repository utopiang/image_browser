"""Geometry comparison and viewport rendering without UI dependencies."""
from __future__ import annotations
import math
from PIL import Image
from .annotation_formats import bounds


def shape_key(shape):
    return (shape.class_id, shape.class_name, shape.kind,
            tuple(tuple(p) for p in shape.points), repr(shape.metadata.get("coco_rle")))


def overlap(a, b):
    """Axis boxes are exact; rotated/concave polygons use bounded local masks."""
    import cv2
    import numpy as np
    ax, ay, ar, ab = bounds(a.points)
    bx, by, br, bb = bounds(b.points)
    iw, ih = min(ar, br)-max(ax, bx), min(ab, bb)-max(ay, by)
    if iw <= 0 or ih <= 0:
        return 0.
    if a.kind == b.kind == "detect":
        inter = iw*ih
        return inter / max((ar-ax)*(ab-ay)+(br-bx)*(bb-by)-inter, 1e-12)
    x, y, r, bottom = min(ax,bx), min(ay,by), max(ar,br), max(ab,bb)
    masks=[]
    for shape in (a,b):
        mask=np.zeros((256,256),np.uint8)
        points=np.asarray([((px-x)/max(r-x,1e-12)*255,
                            (py-y)/max(bottom-y,1e-12)*255) for px,py in shape.points],np.int32)
        cv2.fillPoly(mask,[points],1)
        masks.append(mask)
    union=np.count_nonzero(masks[0] | masks[1])
    return float(np.count_nonzero(masks[0] & masks[1]))/max(union,1)


def compare_shapes(gt, pred, threshold=.65):
    """One-to-one spatial pairing, including class disagreements; no verdicts."""
    abnormal={id(s) for s in gt+pred}
    pairs={}
    candidates=[]
    for a in gt:
        if len(a.points)<3: continue
        for b in pred:
            if len(b.points)<3: continue
            score=overlap(a,b)
            if score >= .1:
                candidates.append((score,a,b))
    used=set()
    for score,a,b in sorted(candidates,key=lambda v:v[0],reverse=True):
        if id(a) in used or id(b) in used: continue
        used.update((id(a),id(b)))
        pairs[id(a)]=b; pairs[id(b)]=a
        if score >= threshold and a.class_name == b.class_name and a.kind == b.kind:
            # Holed masks require human review; their exterior alone is insufficient.
            if not (a.metadata.get("rle_has_holes") or b.metadata.get("rle_has_holes")):
                abnormal.difference_update((id(a),id(b)))
    return abnormal,pairs


def viewport_spec(image_size, original_size, zoom, pan, canvas_size):
    w,h=original_size; px,py=pan; cw,ch=canvas_size
    right,bottom=w*zoom,h*zoom
    x0,y0=max(0,math.floor(px)),max(0,math.floor(py))
    x1,y1=min(cw,math.ceil(px+right)),min(ch,math.ceil(py+bottom))
    if x1<=x0 or y1<=y0: return None
    iw,ih=image_size
    box=((x0-px)/right*iw,(y0-py)/bottom*ih,
         (x1-px)/right*iw,(y1-py)/bottom*ih)
    return (x0,y0),(x1-x0,y1-y0),box


def render_viewport(image, spec, fast=False):
    _,size,box=spec
    # EXTENT samples only the source region; output is bounded by viewport dimensions.
    return image.transform(size,Image.Transform.EXTENT,box,
                           Image.Resampling.BILINEAR if fast else Image.Resampling.BICUBIC)
