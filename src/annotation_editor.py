"""常用标注交互：选择、HBB、OBB、polygon，独立于文件格式。"""
import copy
import math
import tkinter as tk
from tkinter import ttk

try:
    from .annotation_formats import LabelShape, bounds, rectangle
except ImportError:
    from annotation_formats import LabelShape, bounds, rectangle


class AnnotationEditorMixin:
    EDITABLE_KINDS = {"detect", "obb", "segment"}
    TOOL_KINDS = {"水平框 HBB": "detect", "旋转框 OBB": "obb", "多边形分割": "segment"}

    def _editor_tool_changed(self, _event=None):
        self.edit_mode_var.set(self.editor_tool_var.get() != "浏览")
        self._on_edit_mode_changed()

    def _editor_cancel_drawing(self):
        self._polygon_points = []
        self._edit_drag = None
        self.canvas.delete("polygon_preview")
        if self._rubber_id is not None:
            self.canvas.delete(self._rubber_id)
            self._rubber_id = None

    def _editor_press(self, event):
        if not self.current_image:
            return
        self.canvas.focus_set()
        mx, my = event.x, event.y
        if not self.edit_mode_var.get():
            self._edit_drag = {"mode": "pan", "x": mx, "y": my}
            return
        tool = self.editor_tool_var.get()
        if self._edit_sel is not None and self._edit_sel.metadata.get("rle_has_holes") and getattr(event, "state", 0) & (1 | 4):
            self.notify_flow("[编辑] 含孔洞的 RLE 保留原始 mask，可改类别或删除整个实例；轮廓暂不编辑")
            return
        if tool == "选择 / 编辑" and self._edit_sel is not None and self._edit_sel.kind == "segment" and getattr(event, "state", 0) & 1:
            point = self._canvas_to_norm(mx, my)
            pts = self._edit_sel.points
            def distance_to_edge(index):
                ax, ay = self._editor_to_canvas(pts[index])
                bx, by = self._editor_to_canvas(pts[(index + 1) % len(pts)])
                dx, dy = bx - ax, by - ay
                t = max(0., min(1., ((mx-ax)*dx+(my-ay)*dy) / max(dx*dx+dy*dy, 1e-8)))
                return math.hypot(mx-ax-t*dx, my-ay-t*dy)
            edge = min(range(len(pts)), key=distance_to_edge)
            if distance_to_edge(edge) <= 12:
                self._push_edit_undo()
                pts.insert(edge + 1, point)
                self._edit_dirty = True
                self._schedule_interaction_render()
            return
        if tool == "多边形分割":
            point = self._canvas_to_norm(mx, my)
            if not self._point_inside_image(point):
                return
            pending = getattr(self, "_polygon_points", [])
            if len(pending) >= 3:
                x0, y0 = self._editor_to_canvas(pending[0])
                if math.hypot(mx - x0, my - y0) < 9:
                    self._editor_finish_polygon()
                    return
            if not pending or math.dist(point, pending[-1]) > 1e-6:
                pending.append(point)
            self._polygon_points = pending
            self._editor_polygon_preview()
            return
        if tool in self.TOOL_KINDS:
            point = self._canvas_to_norm(mx, my)
            if not self._point_inside_image(point):
                return
            self._edit_sel = None
            self._focus_shape = None
            self._edit_drag = {"mode": "new", "x0": mx, "y0": my, "kind": self.TOOL_KINDS[tool]}
            self._rubber_id = self.canvas.create_rectangle(mx, my, mx, my, outline="#ffffff", dash=(4, 4))
            return
        hits = self.canvas.find_overlapping(mx - 4, my - 4, mx + 4, my + 4)
        for iid in reversed(hits):
            tags = self.canvas.gettags(iid)
            if self._edit_sel is None:
                break
            handle = next((tag for tag in tags if tag.startswith("handle")), None)
            if handle:
                index = int(handle[6:])
                if self._edit_sel.kind == "segment" and getattr(event, "state", 0) & 4:
                    if len(self._edit_sel.points) > 3:
                        self._push_edit_undo()
                        self._edit_sel.points.pop(index)
                        self._edit_dirty = True
                        self._schedule_interaction_render()
                    return
                self._push_edit_undo()
                self._edit_drag = {"mode": "resize", "handle": index, "shape": self._edit_sel,
                                   "start_points": list(self._edit_sel.points)}
                return
            if "rotate_handle" in tags:
                points = self._edit_sel.points
                center = (sum(x for x, _ in points) / 4, sum(y for _, y in points) / 4)
                cx, cy = self._editor_to_canvas(center)
                self._push_edit_undo()
                self._edit_drag = {"mode": "rotate", "shape": self._edit_sel, "center": center,
                                   "angle": math.atan2(my - cy, mx - cx), "start_points": list(points)}
                return
        shape = self._pick_canvas_shape(mx, my)
        self._focus_shape = shape
        self._edit_sel = shape if shape in self._edit_shapes else None
        if self._edit_sel is not None and not shape.metadata.get("rle_has_holes"):
            self._push_edit_undo()
            self._edit_drag = {"mode": "move", "shape": shape, "start_norm": self._canvas_to_norm(mx, my),
                               "start_points": list(shape.points)}
        elif self._edit_sel is None:
            self._edit_drag = {"mode": "pan", "x": mx, "y": my}
        self.render_image()

    def _editor_drag(self, event):
        drag = self._edit_drag
        if not drag:
            return
        mode = drag["mode"]
        if mode == "pan":
            self.pan_x += event.x - drag["x"]
            self.pan_y += event.y - drag["y"]
            drag["x"], drag["y"] = event.x, event.y
            self._schedule_interaction_render()
        elif mode == "new":
            self.canvas.coords(self._rubber_id, drag["x0"], drag["y0"], event.x, event.y)
        elif mode == "move":
            nx, ny = self._canvas_to_norm(event.x, event.y)
            dx, dy = nx - drag["start_norm"][0], ny - drag["start_norm"][1]
            x1, y1, x2, y2 = bounds(drag["start_points"])
            dx, dy = max(-x1, min(1 - x2, dx)), max(-y1, min(1 - y2, dy))
            drag["shape"].points = [(x + dx, y + dy) for x, y in drag["start_points"]]
            self._edit_dirty = True
            self._schedule_interaction_render()
        elif mode == "resize":
            self._apply_handle_resize(drag["shape"], drag["handle"], self._canvas_to_norm(event.x, event.y))
            self._edit_dirty = True
            self._schedule_interaction_render()
        elif mode == "rotate":
            cx, cy = self._editor_to_canvas(drag["center"])
            angle = math.atan2(event.y - cy, event.x - cx) - drag["angle"]
            c, s = math.cos(angle), math.sin(angle)
            result = []
            for point in drag["start_points"]:
                px, py = self._editor_to_canvas(point)
                result.append(self._canvas_to_norm(cx + (px - cx) * c - (py - cy) * s,
                                                   cy + (px - cx) * s + (py - cy) * c))
            if all(self._point_inside_image(p) for p in result):
                drag["shape"].points = result
                self._edit_dirty = True
                self._schedule_interaction_render()

    def _editor_release(self, event):
        drag = self._edit_drag
        self._edit_drag = None
        if drag and drag["mode"] != "new":
            self.render_image()
        if drag and drag["mode"] == "new":
            if self._rubber_id is not None:
                self.canvas.delete(self._rubber_id)
                self._rubber_id = None
            if abs(event.x - drag["x0"]) >= 4 and abs(event.y - drag["y0"]) >= 4:
                a = self._canvas_to_norm(drag["x0"], drag["y0"])
                b = self._canvas_to_norm(event.x, event.y)
                b = (max(0., min(1., b[0])), max(0., min(1., b[1])))
                self._editor_commit(drag["kind"], rectangle(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))

    def _editor_commit(self, kind, points):
        from pathlib import Path
        cid = self._class_pick_dialog("选择标注类别", current_class_id=self._draw_class_id)
        if cid is None:
            self._editor_cancel_drawing()
            return
        self._draw_class_id = cid
        if not self._has_label_source():
            # 新标注默认使用原生 JSON，可混合保存三种类型。
            self.label_dir_var.set(str(Path(self.image_paths[self.current_index]).parent))
            self.label_format_var.set("labelme")
        self._push_edit_undo()
        shape = LabelShape(cid, self.label_store.ensure_class_id(cid), kind, list(points))
        self._edit_shapes.append(shape)
        self._edit_sel = self._focus_shape = shape
        self._edit_dirty = True
        self.editor_tool_var.set("选择 / 编辑")
        self._polygon_points = []
        self._editor_update_hint()
        self.render_image()

    def _editor_finish_polygon(self, _event=None):
        points = getattr(self, "_polygon_points", [])
        if len(points) >= 3:
            self._editor_cancel_drawing()
            self._editor_commit("segment", points)
        return "break"

    def _editor_polygon_preview(self, event=None):
        points = getattr(self, "_polygon_points", [])
        if not points:
            return
        self.canvas.delete("polygon_preview")
        coords = [v for p in points for v in self._editor_to_canvas(p)]
        if event is not None:
            coords += [event.x, event.y]
        if len(coords) >= 4:
            self.canvas.create_line(coords, fill="#ffffff", width=2, dash=(4, 3), tags="polygon_preview")
        x, y = self._editor_to_canvas(points[0])
        self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#3b82f6", outline="#ffffff", tags="polygon_preview")

    def _editor_draw_handles(self, shape):
        if not self.edit_mode_var.get() or shape is None or shape.metadata.get("rle_has_holes"):
            return
        points = [self._editor_to_canvas(p) for p in shape.points]
        coords = [v for p in points for v in p]
        self._handle_items.append(self.canvas.create_line(coords + coords[:2], fill="white", width=1))
        handles = list(points)
        if shape.kind == "detect" and len(points) == 4:
            handles += [((points[i][0] + points[(i+1)%4][0])/2, (points[i][1] + points[(i+1)%4][1])/2) for i in range(4)]
        for index, (x, y) in enumerate(handles):
            self._handle_items.append(self.canvas.create_rectangle(x - 4, y - 4, x + 4, y + 4, fill="white", outline="#2563eb", tags=f"handle{index}"))
        if shape.kind == "obb" and len(points) == 4:
            cx, cy = sum(x for x, _ in points)/4, sum(y for _, y in points)/4
            mx, my = (points[0][0]+points[1][0])/2, (points[0][1]+points[1][1])/2
            length = max(math.hypot(mx-cx, my-cy), 1)
            x, y = mx+(mx-cx)/length*28, my+(my-cy)/length*28
            self._handle_items.append(self.canvas.create_line(mx, my, x, y, fill="#ffffff"))
            self._handle_items.append(self.canvas.create_oval(x-6, y-6, x+6, y+6, fill="#3b82f6", outline="white", tags="rotate_handle"))

    def _editor_resize(self, shape, index, point):
        if shape.metadata.get("rle_has_holes"):
            return
        point = (max(0., min(1., point[0])), max(0., min(1., point[1])))
        if shape.kind == "segment":
            shape.points[index] = point
        elif shape.kind == "obb":
            # 在旋转框的局部正交坐标中调整尺寸，保持四角垂直。
            points = [self._editor_to_canvas(p) for p in shape.points]
            opposite = points[(index + 2) % 4]
            edge = (points[1][0]-points[0][0], points[1][1]-points[0][1])
            norm = max(math.hypot(*edge), 1e-8)
            ux, uy = edge[0]/norm, edge[1]/norm
            vx, vy = -uy, ux
            px, py = self._editor_to_canvas(point)
            dx, dy = px-opposite[0], py-opposite[1]
            du, dv = dx*ux+dy*uy, dx*vx+dy*vy
            # 原顶点顺序可能来自 minAreaRect，按照相邻边重建。
            adjacent_u = (opposite[0]+du*ux, opposite[1]+du*uy)
            adjacent_v = (opposite[0]+dv*vx, opposite[1]+dv*vy)
            result = [None]*4
            result[index] = (px, py)
            result[(index+2)%4] = opposite
            a = points[(index+1)%4]
            result[(index+1)%4], result[(index+3)%4] = (adjacent_u, adjacent_v) if math.dist(a, adjacent_u) < math.dist(a, adjacent_v) else (adjacent_v, adjacent_u)
            normalized = [self._canvas_to_norm(*p) for p in result]
            if all(self._point_inside_image(p) for p in normalized):
                shape.points = normalized
        else:
            x1, y1, x2, y2 = bounds(shape.points)
            x, y = point
            if index in {0, 3, 7}: x1 = x
            if index in {1, 2, 5}: x2 = x
            if index in {0, 1, 4}: y1 = y
            if index in {2, 3, 6}: y2 = y
            shape.points = rectangle(min(x1,x2), min(y1,y2), max(x1,x2), max(y1,y2))

    def _editor_to_canvas(self, point):
        path = self.image_paths[self.current_index]
        w, h = self._original_sizes.get(path, self.current_image.size)
        return self.pan_x + point[0]*w*self.zoom, self.pan_y + point[1]*h*self.zoom

    @staticmethod
    def _point_inside_image(point):
        return 0 <= point[0] <= 1 and 0 <= point[1] <= 1

    def _editor_update_hint(self):
        tool = self.editor_tool_var.get()
        hint = {"浏览": "滚轮缩放 · 左键拖动 · A / D 切图 · F 适应窗口",
                "选择 / 编辑": "选择标注 · 拖动顶点修改 · OBB 蓝色手柄旋转 · Delete 删除 · Ctrl+Z 撤销",
                "多边形分割": "逐点单击 · 双击 / Enter / 点击起点闭合 · Esc 取消",
                "水平框 HBB": "拖动绘制水平框 · 绘制后自动切回选择工具",
                "旋转框 OBB": "拖动画框后，用蓝色旋转手柄调整角度"}
        self._preview_hint_var.set(hint.get(tool, ""))
        self.canvas.configure(cursor="" if tool in {"浏览", "选择 / 编辑"} else "crosshair")

    def _editor_sync_object_list(self):
        if not hasattr(self, "_object_tree"):
            return
        shapes = list(self._edit_shapes) + list(getattr(self, "_cur_pred_shapes", []))
        signature = tuple((id(s), s.class_name, s.kind, s.confidence) for s in shapes)
        if signature != getattr(self, "_object_list_signature", None):
            self._object_list_signature = signature
            self._object_tree.delete(*self._object_tree.get_children())
            self._object_shapes = shapes
            for i, shape in enumerate(shapes):
                kind = {"detect": "HBB", "obb": "OBB", "segment": "分割"}.get(shape.kind, shape.kind)
                source = "G" if any(shape is s for s in self._edit_shapes) else "P"
                self._object_tree.insert("", "end", iid=str(i), values=(shape.class_name, kind, source))
        focus = self._focus_shape
        selected = next((str(i) for i,s in enumerate(shapes) if s is focus), None)
        if selected is not None and self._object_tree.selection() != (selected,):
            self._object_tree.selection_set(selected)
        elif selected is None and self._object_tree.selection():
            self._object_tree.selection_remove(*self._object_tree.selection())
        if hasattr(self, "_selection_detail"):
            text = "选择标注查看详情"
            if focus is not None:
                source = "人工" if any(focus is s for s in self._edit_shapes) else "预测"
                score = f" · 置信度 {focus.confidence:.3f}" if focus.confidence is not None else ""
                text = f"{source} · {focus.class_name}{score}"
            if getattr(self,"_comparison_pending",False): text += " · 核对中"
            self._selection_detail.set(text)
        self._refresh_context_actions()

    def _editor_select_object(self, _event=None):
        selection = self._object_tree.selection()
        if selection:
            shape = self._object_shapes[int(selection[0])]
            if shape is not self._focus_shape:
                self._focus_shape = shape
                self._edit_sel = shape if any(shape is s for s in self._edit_shapes) else None
                self.editor_tool_var.set("选择 / 编辑")
                self.edit_mode_var.set(True)
                self._editor_update_hint()
                self._center_on_shape(shape)
                self._schedule_interaction_render()

    def _editor_edit_class(self, _event=None):
        if self._edit_sel is None:
            return
        cid = self._class_pick_dialog("修改类别", current_class_id=self._edit_sel.class_id)
        if cid is not None:
            self._edit_set_class(cid)
            self._sync_class_filter()
            self._update_draw_class_combo()

    def _editor_add_class(self):
        cid = self._class_pick_dialog("添加标注类别")
        if cid is not None:
            self._draw_class_id = cid
            self._sync_class_filter()
            self._update_draw_class_combo()
