"""工作台交互回归：使用临时图片和真实 Tk 控件，不接触用户数据。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from src import tk_app


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="image_browser_test_")
        self.base = Path(self.temp.name)
        self.config_dir = patch.object(tk_app, "CONFIG_DIR", self.base)
        self.config_path = patch.object(tk_app, "CONFIG_PATH", self.base / "config.json")
        self.config_dir.start()
        self.config_path.start()
        self.app = tk_app.TkImageBrowser(visible=False)
        self.app._show_task_restore_prompt = lambda: None
        self.errors = []
        self.app.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.app._preload_surrounding = lambda *args: None
        self.app._schedule_pre_render = lambda: None
        self.class_picker = patch.object(self.app, "_class_pick_dialog", return_value=0)
        self.class_picker.start()
        self.images = self.base / "images"
        self.labels = self.base / "labels"
        self.images.mkdir()
        self.labels.mkdir()
        self.paths = []
        for i, name in enumerate(["tunnel_01.jpg", "tunnel_02.jpg", "crack_03.jpg", "tunnel_04.jpg"]):
            path = self.images / name
            Image.new("RGB", (320, 180), (70 + i * 10, 80, 90)).save(path)
            self.paths.append(str(path.resolve()))
            (self.labels / f"{path.stem}.txt").write_text("0 0.5 0.5 0.4 0.4\n", encoding="utf-8")
        self.app.dir_var.set(str(self.images))
        self.app.label_dir_var.set(str(self.labels))
        self.app.label_store.classes = ["crack"]
        self.app.image_paths = list(self.paths)
        for path in self.paths:
            self.app._image_cache[path] = Image.new("RGB", (320, 180))
            self.app._original_sizes[path] = (320, 180)
        self.app.current_index = 0
        self.app.show_current_image()
        self.app.root.update()

    def tearDown(self):
        self.app._edit_dirty = False
        with patch("src.tk_app.threading.Timer"):
            self.app._on_close()
        self.class_picker.stop()
        self.config_path.stop()
        self.config_dir.stop()
        self.temp.cleanup()
        self.assertEqual(self.errors, [], "Tk 回调不应出现异常")

    def test_cancel_new_shape_keeps_annotations_and_undo(self):
        before=self.app._edit_snapshot()
        self.app._class_pick_dialog.return_value=None
        self.app._editor_commit("detect",[(.1,.1),(.2,.1),(.2,.2),(.1,.2)])
        self.assertEqual(self.app._edit_shapes,before)
        self.assertFalse(self.app._edit_undo)
        self.assertFalse(hasattr(self.app,"_draw_class_combo"))

    def test_prediction_review_accept_and_undo(self):
        predictions=self.base/"predictions";predictions.mkdir()
        (predictions/"tunnel_01.txt").write_text("0 0.2 0.2 0.1 0.1 0.9\n",encoding="utf-8")
        self.app.pred_label_dir_var.set(str(predictions))
        self.app.render_image()
        prediction=self.app._cur_pred_shapes[0]
        self.assertTrue(self.app._needs_review(prediction))
        self.app._focus_shape=prediction
        self.app._confirm_review()
        self.assertFalse(self.app._needs_review(prediction))
        before=self.app._edit_snapshot()
        self.app._accept_prediction()
        self.assertEqual(len(self.app._edit_shapes),len(before)+1)
        self.assertTrue(self.app._edit_dirty)
        self.app._edit_undo_pop()
        self.assertEqual(self.app._edit_shapes,before)
        self.assertTrue(self.app._needs_review(self.app._cur_pred_shapes[0]))
        self.assertFalse(self.app._canvas_label_text_ids)

    def test_undo_history_and_image_cache_have_limits(self):
        for _ in range(40): self.app._push_edit_undo()
        self.assertEqual(len(self.app._edit_undo),30)
        for i in range(12):
            self.app._cache_loaded_image(str(i),Image.new("RGB",(100,100)))
        self.assertLessEqual(len(self.app._image_cache),6)

    def test_async_viewport_discards_obsolete_results(self):
        import time
        self.app.root.geometry("1440x900")
        self.app.root.deiconify();self.app.root.update()
        self.app.zoom=8
        self.app.pan_x=-100;self.app.pan_y=-100
        self.app.render_image()
        self.app.pan_x=-200
        self.app.render_image()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            self.app.root.update()
            if self.app._viewport_key==self.app._viewport_requested: break
            time.sleep(.01)
        self.assertEqual(self.app._viewport_key,self.app._viewport_requested)
        self.assertLessEqual(self.app.tk_image.width(),self.app.canvas.winfo_width())
        self.assertLessEqual(self.app.tk_image.height(),self.app.canvas.winfo_height())

    def test_unified_import_validates_ids_then_reuses_current_classes(self):
        import time
        from tkinter import ttk
        import tkinter as tk
        self.app.show_label_import()
        dialog=next(w for w in self.app.root.winfo_children() if isinstance(w,tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets=list(descendants(dialog))
        entry=next(w for w in widgets if isinstance(w,ttk.Entry) and not isinstance(w,ttk.Combobox))
        entry.insert(0,str(self.labels))
        combos=[w for w in widgets if isinstance(w,ttk.Combobox)]
        combos[-1].set("使用当前类别表")
        button=next(w for w in widgets if isinstance(w,ttk.Button) and w.cget("text")=="导入")
        file=self.labels/"tunnel_01.txt"
        file.write_text("9 0.5 0.5 0.4 0.4\n",encoding="utf-8")
        button.invoke()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and button.instate(["disabled"]):
            self.app.root.update();time.sleep(.01)
        self.assertTrue(dialog.winfo_exists())
        messages=[str(self.app.root.getvar(w.cget("textvariable"))) for w in widgets if isinstance(w,ttk.Label) and w.cget("textvariable")]
        self.assertTrue(any("超出类别表范围" in m for m in messages),messages)
        file.write_text("0 0.5 0.5 0.4 0.4\n",encoding="utf-8")
        button.invoke()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and dialog.winfo_exists():
            self.app.root.update();time.sleep(.01)
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.app.label_store.classes,["crack"])
        self.assertIsNone(self.app.label_store.active_classes)

    def test_overlapping_sources_cycle_without_moving_boxes(self):
        predictions=self.base/"predictions";predictions.mkdir()
        (predictions/"tunnel_01.txt").write_text("0 0.5 0.5 0.4 0.4 0.9\n",encoding="utf-8")
        self.app.pred_label_dir_var.set(str(predictions))
        self.app.render_image()
        x,y=self.app._editor_to_canvas((.5,.5))
        first=self.app._pick_canvas_shape(x,y)
        self.app._focus_shape=first
        second=self.app._pick_canvas_shape(x,y)
        self.assertIsNotNone(first);self.assertIsNotNone(second)
        self.assertIsNot(first,second)
        self.assertEqual(first.points,second.points)

    def test_slider_scrub_only_commits_last_target(self):
        before=self.app.current_index
        self.app._slider_press()
        with patch.object(self.app,"show_current_image",wraps=self.app.show_current_image) as show:
            for value in range(0,1001,10): self.app._on_image_scale_change(str(value))
            self.assertEqual(self.app.current_index,before)
            self.assertEqual(show.call_count,0)
            self.app._slider_release()
            self.assertEqual(show.call_count,1)
            self.assertEqual(self.app.current_index,len(self.paths)-1)
        self.assertIsNone(self.app._slider_target)

    def test_nearby_cache_contains_only_two_small_previews(self):
        import time
        self.app.current_index=1
        self.app._image_loading=False
        self.app._load_nearby_previews()
        deadline=time.monotonic()+3
        while len(self.app._preview_cache)<2 and time.monotonic()<deadline:
            self.app.root.update();time.sleep(.01)
        self.assertEqual(set(self.app._preview_cache),{self.paths[0],self.paths[2]})
        self.app.current_index=2
        self.app._load_nearby_previews()
        deadline=time.monotonic()+3
        while set(self.app._preview_cache)!={self.paths[1],self.paths[3]} and time.monotonic()<deadline:
            self.app.root.update();time.sleep(.01)
        self.assertEqual(set(self.app._preview_cache),{self.paths[1],self.paths[3]})

    def test_async_original_loader_keeps_only_current_full_image(self):
        import time
        self.app._image_cache.clear()
        self.app.current_index=2
        self.app.show_current_image()
        self.app.current_index=3
        self.app.show_current_image()
        deadline=time.monotonic()+3
        while (self.app._image_loading or self.app.current_image is None) and time.monotonic()<deadline:
            self.app.root.update();time.sleep(.01)
        self.assertEqual(set(self.app._image_cache),{self.paths[3]})
        self.assertIs(self.app.current_image,self.app._image_cache[self.paths[3]])

    def test_context_buttons_show_only_for_applicable_selection(self):
        self.app._focus_shape=None
        self.app._refresh_context_actions()
        self.assertFalse(self.app._accept_prediction_button.winfo_manager())
        self.assertFalse(self.app._confirm_review_button.winfo_manager())
        self.app._scan_active=True
        self.app._refresh_context_actions()
        self.assertEqual(self.app._cancel_scan_button.winfo_manager(),"pack")
        self.app._scan_active=False
        self.app._refresh_context_actions()
        self.assertFalse(self.app._cancel_scan_button.winfo_manager())

    def test_recovery_draft_can_be_restored_without_overwriting_labels(self):
        import time
        shape=self.app._edit_shapes[0]
        shape.class_name="water"
        original=(self.labels/"tunnel_01.txt").read_text(encoding="utf-8")
        self.app._draft_store.save(self.paths[0],str(self.labels),[shape])
        draft=self.app._draft_store.path_for(self.paths[0],str(self.labels))
        deadline=time.monotonic()+2
        while not draft.exists() and time.monotonic()<deadline: time.sleep(.01)
        self.app._draft_seen.clear()
        self.app._edit_dirty=False
        with patch("src.review_ui.messagebox.askyesnocancel",return_value=True):
            self.app._offer_recovery_draft(self.paths[0])
        self.assertTrue(self.app._edit_dirty)
        self.assertEqual(self.app._edit_shapes[0].class_name,"water")
        self.assertEqual((self.labels/"tunnel_01.txt").read_text(encoding="utf-8"),original)

    def test_panning_reuses_annotation_canvas_items(self):
        self.app.render_image()
        before=set(self.app._canvas_label_ids)
        self.app.pan_x+=1
        self.app.render_image()
        self.assertEqual(set(self.app._canvas_label_ids),before)

    def test_large_comparison_uses_worker_and_ignores_old_result(self):
        import copy,time
        original=self.app._edit_shapes[0]
        gt=[copy.deepcopy(original) for _ in range(9)]
        pred=[copy.deepcopy(original) for _ in range(9)]
        self.app.pred_label_dir_var.set(str(self.labels))
        self.app._update_review_comparison(gt,pred,self.paths[0])
        ticket=self.app._comparison_ticket
        self.assertTrue(self.app._comparison_pending)
        self.app._update_review_comparison([],[],self.paths[1])
        self.app._apply_review_result((ticket,{id(original)},{}))
        self.assertFalse(self.app._review_abnormal)

    def test_repeated_full_image_jumps_leave_bounded_cache_and_stop_workers(self):
        import time
        self.app._image_cache.clear()
        for _ in range(5):
            self.app._slider_press()
            for value in (100,900,300,700,1000):
                self.app._on_image_scale_change(str(value))
            self.app._slider_release()
            deadline=time.monotonic()+2
            while self.app._image_loading and time.monotonic()<deadline:
                self.app.root.update();time.sleep(.01)
            self.assertLessEqual(len(self.app._image_cache),1)
        self.app._close_image_pipeline()
        for worker in (self.app._full_loader,self.app._preview_loader):
            worker.thread.join(1)
            self.assertFalse(worker.thread.is_alive())

    def test_sections_share_picture_and_return_from_tools(self):
        self.app.current_index = 2
        self.app.show_current_image()
        for section in ["labels", "marks", "browse"]:
            self.app.show_panel(section)
            self.assertEqual(self.app.current_index, 2)
            # body 只剩 中央画布 + 右栏（图片库在右栏下半区，随模块切换显隐）
            self.assertEqual(len(self.app.body.panes()), 3)
            self.assertEqual(self.app._tool_bar.winfo_manager(), "pack")
            self.assertIn(str(self.app._library_pane), self.app.body.panes())
        self.app.show_panel("video")
        self.assertEqual(len(self.app.body.panes()), 1)
        self.assertEqual(self.app._tool_bar.winfo_manager(), "")
        self.app.show_panel("dedupe")
        self.assertEqual(len(self.app.body.panes()), 2)
        self.assertNotIn(str(self.app._library_pane), self.app.body.panes())
        self.app.image_paths = ["dedupe-result.jpg"]
        self.app.current_index = 0
        self.app.show_panel("workspace")
        self.assertEqual(self.app.image_paths, self.paths)
        self.assertEqual(self.app.current_index, 2)

    def test_search_and_next_use_filtered_results(self):
        self.app._image_search_var.set("TUNNEL")
        self.app._apply_library_filter()
        self.assertEqual(self.app._library_indices, [0, 1, 3])
        self.app.next_image()
        self.app.next_image()
        self.assertEqual(self.app.current_index, 3)
        self.app.prev_image()
        self.assertEqual(self.app.current_index, 1)
        self.app._image_search_var.set("does-not-exist")
        self.app._apply_library_filter()
        self.assertEqual(self.app._library_tree.get_children(), ())
        self.assertIsNone(self.app._navigation_target(1))

    def test_large_library_is_bounded_and_follows_current_page(self):
        self.app.image_paths = [str(self.images / f"image_{i:05}.jpg") for i in range(10000)]
        self.app._refresh_workspace(rebuild=True)
        self.assertEqual(len(self.app._library_tree.get_children()), 200)
        self.app.current_index = 9900
        self.app._render_library_page(follow_current=True)
        self.assertEqual(self.app._library_page, 49)
        self.assertEqual(self.app._library_tree.selection(), ("9900",))

    def test_browsing_drag_does_not_edit_labels(self):
        before = self.app._edit_snapshot()
        self.app._on_canvas_press(SimpleNamespace(x=20, y=20))
        self.app._on_canvas_drag(SimpleNamespace(x=80, y=60))
        self.app._on_canvas_release(SimpleNamespace(x=80, y=60))
        self.assertEqual(before, self.app._edit_snapshot())
        self.assertFalse(self.app._edit_dirty)

    def test_mark_auto_next_is_explicit(self):
        category = self.app._categories()[0]
        self.app.toggle_mark(category)
        self.assertEqual(self.app.current_index, 0)
        self.app.toggle_mark(category)
        self.app.mark_auto_next_var.set(True)
        self.app.toggle_mark(category)
        self.assertEqual(self.app.current_index, 1)
        self.assertIn(self.paths[0], (self.images / f"{category}.txt").read_text(encoding="utf-8"))

    def test_auto_save_on_switch_persists_and_advances(self):
        # 自动保存常开：修改后切图直接保存（内容写入标签文件）并前进
        self.app._edit_shapes[0].points[0] = (.9, .9)
        self.app._edit_dirty = True
        self.app.next_image()
        self.assertEqual(self.app.current_index, 1)
        self.assertFalse(self.app._edit_dirty)
        self.assertIn("0.60", (self.labels / "tunnel_01.txt").read_text(encoding="utf-8"))

    def test_auto_save_writes_labels_before_leaving(self):
        self.app._edit_shapes = []
        self.app._edit_dirty = True
        self.app.next_image()
        self.assertEqual(self.app.current_index, 1)
        self.assertFalse(self.app._edit_dirty)
        self.assertEqual((self.labels / "tunnel_01.txt").read_text(encoding="utf-8"), "")

    def test_save_failure_blocks_navigation(self):
        self.app._edit_dirty = True
        with patch.object(tk_app, "write_yolo_label_file", return_value=False):
            self.app.next_image()
        self.assertEqual(self.app.current_index, 0)
        self.assertTrue(self.app._edit_dirty)

    def test_background_dedupe_does_not_replace_workbench(self):
        before = self.app.image_paths
        self.app._dedupe_task_states[99] = {"image_paths": ["other.jpg"]}
        self.app._display_dedupe_task(99)
        self.assertIs(self.app.image_paths, before)
        self.assertEqual(self.app._current_module, "browse")

    def test_overview_and_empty_state_refresh(self):
        self.assertEqual(self.app._loaded_count_var.get(), "4")
        self.assertIn("320 × 180", self.app._img_info_text.get("1.0", "end"))
        self.app.image_paths = []
        self.app.current_index = -1
        self.app.show_current_image()
        self.assertEqual(self.app._empty_state.winfo_manager(), "place")
        self.assertEqual(self.app._total_label.cget("text"), " / 0")

    def test_label_matching_and_auto_classes(self):
        (self.labels / "classes.txt").write_text("crack\nseepage\n", encoding="utf-8")
        self.app._auto_detect_label_dir(self.paths)
        self.app.root.update()
        self.assertEqual(self.app.label_store.classes, ["crack", "seepage"])

    def test_fixed_action_bars_and_preferences(self):
        self.app.root.geometry("1040x700")
        for key in ["labels", "marks", "video", "dedupe", "browse"]:
            self.app.show_panel(key)
            self.app.root.update()
            if key in {"labels", "marks"}:
                bar = self.app._panels[key].action_bar
                self.assertGreater(bar.winfo_reqheight(), 20)
                self.assertEqual(bar.pack_info()["side"], "bottom")
        self.app._save_config()
        self.assertIn("mark_auto_next", json.loads(tk_app.CONFIG_PATH.read_text(encoding="utf-8")))

    def select_tool(self, tool):
        self.app._auto_fit.set(False)
        self.app.zoom = 1.
        self.app.pan_x = self.app.pan_y = 0.
        self.app.editor_tool_var.set(tool)
        self.app._editor_tool_changed()

    def test_hbb_create_and_undo_preserves_original_shapes(self):
        before = self.app._edit_snapshot()
        self.select_tool("水平框 HBB")
        self.app._on_canvas_press(SimpleNamespace(x=20,y=20))
        self.app._on_canvas_drag(SimpleNamespace(x=120,y=100))
        self.app._on_canvas_release(SimpleNamespace(x=120,y=100))
        self.assertEqual(len(self.app._edit_shapes), len(before)+1)
        self.assertEqual(self.app.editor_tool_var.get(), "选择 / 编辑")
        self.app._edit_undo_pop()
        self.assertEqual(self.app._edit_shapes, before)

    def test_polygon_create_move_vertex_and_undo(self):
        self.select_tool("多边形分割")
        for x,y in [(20,20),(120,20),(110,100),(30,90)]:
            self.app._on_canvas_press(SimpleNamespace(x=x,y=y))
        self.app._editor_finish_polygon()
        polygon=self.app._edit_shapes[-1]
        self.assertEqual(polygon.kind,"segment")
        self.assertEqual(len(polygon.points),4)
        self.app._edit_undo.append(self.app._edit_snapshot())
        self.app._apply_handle_resize(polygon,2,(.5,.6))
        self.assertEqual(polygon.points[2],(.5,.6))
        self.app._edit_undo_pop()
        self.assertAlmostEqual(self.app._edit_shapes[-1].points[2][0],110/320)

    def test_obb_rotation_and_resize_keep_right_angles(self):
        import math
        from src.annotation_formats import LabelShape
        self.select_tool("选择 / 编辑")
        shape=LabelShape(0,"crack","obb",[(.3,.3),(.6,.3),(.6,.6),(.3,.6)])
        self.app._edit_shapes.append(shape)
        self.app._edit_dirty=True
        self.app._edit_sel=shape
        center=(.45,.45)
        cx,cy=self.app._editor_to_canvas(center)
        self.app._edit_drag={"mode":"rotate","shape":shape,"center":center,"angle":0.,"start_points":list(shape.points)}
        self.app._editor_drag(SimpleNamespace(x=cx+20,y=cy+20))
        def perpendicular(points):
            pixels=[(x*320,y*180) for x,y in points]
            u=(pixels[1][0]-pixels[0][0],pixels[1][1]-pixels[0][1])
            v=(pixels[2][0]-pixels[1][0],pixels[2][1]-pixels[1][1])
            self.assertAlmostEqual(u[0]*v[0]+u[1]*v[1],0.,places=5)
        perpendicular(shape.points)
        self.app._apply_handle_resize(shape,0,(.25,.3))
        perpendicular(shape.points)

    def test_labelme_editor_saves_original_json_and_metadata(self):
        from src.annotation_formats import atomic_json
        path=self.labels/"tunnel_01.json"
        atomic_json(path,{"imageWidth":320,"imageHeight":180,"flags":{"reviewed":True},"shapes":[
            {"label":"crack","shape_type":"polygon","points":[[20,20],[100,20],[90,90]],"group_id":4,"flags":{"wet":True}}]})
        self.app.label_format_var.set("labelme")
        self.app.label_store.clear_label_cache()
        self.app._label_cache.clear()
        self.app.show_current_image()
        self.app._edit_shapes[0].points[0]=(.1,.1)
        self.app._edit_dirty=True
        self.app._save_current_labels(force=True)
        saved=json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["shapes"][0]["points"][0],[32.,18.])
        self.assertEqual(saved["flags"],{"reviewed":True})
        self.assertEqual(saved["shapes"][0]["group_id"],4)

    def test_hidden_class_is_preserved_when_saving(self):
        p=self.labels/"tunnel_01.txt"
        p.write_text("0 .2 .2 .2 .2\n1 .7 .7 .2 .2\n",encoding="utf-8")
        self.app.label_store.classes=["crack","seepage"]
        self.app.label_store.active_classes={0}
        self.app.label_store.clear_label_cache()
        self.app._label_cache.clear()
        self.app.show_current_image()
        self.assertEqual(len(self.app._edit_shapes),2)
        self.app._edit_shapes[0].points[0]=(.11,.11)
        self.app._edit_dirty=True
        self.app._save_current_labels(force=True)
        self.assertEqual(len(p.read_text().splitlines()),2)

    def test_hidden_labels_can_still_be_exported(self):
        self.app.labels_visible.set(False)
        self.app.render_image()
        self.assertEqual(len(self.app._edit_shapes),1)

    def test_auto_saved_edits_persist_across_switch(self):
        # 自动保存常开：修改后切图再切回，编辑几何已持久化（HBB 按包围盒规范化）
        self.app._edit_shapes[0].points[0]=(.9,.9)
        self.app._edit_dirty=True
        self.app.next_image()
        self.app.prev_image()
        pts = self.app._edit_shapes[0].points
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        self.assertAlmostEqual(max(xs), .9); self.assertAlmostEqual(max(ys), .9)
        self.assertAlmostEqual(min(xs), .3); self.assertAlmostEqual(min(ys), .3)
        self.assertFalse(self.app._edit_dirty)

    def test_icon_tools_use_shared_images_and_selected_state(self):
        self.assertIs(self.app._icons.get("polygon"), self.app._icons.get("polygon"))
        self.assertEqual(self.app._icons.get("polygon").width(),22)
        self.assertEqual(len(self.app._editor_tool_buttons),5)
        self.select_tool("多边形分割")
        self.app.root.update_idletasks()
        self.assertTrue(self.app._editor_tool_buttons["多边形分割"].instate(["selected"]))

    def test_coco_editor_save_preserves_other_image_annotations(self):
        from src.annotation_formats import atomic_json
        source=self.base/"annotations.json"
        doc={"images":[{"id":7,"file_name":Path(self.paths[0]).name,"width":320,"height":180},
                       {"id":8,"file_name":Path(self.paths[1]).name,"width":320,"height":180}],
             "categories":[{"id":42,"name":"crack"}],
             "annotations":[{"id":1,"image_id":7,"category_id":42,"bbox":[10,20,100,70],"iscrowd":0},
                            {"id":2,"image_id":8,"category_id":42,"bbox":[20,30,50,60],"custom":"preserve"}]}
        atomic_json(source,doc)
        self.app.label_store.load_coco(str(source))
        self.app.label_format_var.set("coco")
        self.app.coco_file_var.set(str(source))
        self.app._label_cache.clear()
        self.app.show_current_image()
        self.app._edit_shapes[0].points=[(.1,.1),(.5,.1),(.5,.5),(.1,.5)]
        self.app._edit_dirty=True
        self.app._save_current_labels(force=True)
        self.assertFalse(self.app._edit_dirty)
        saved=json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual(next(a for a in saved["annotations"] if a["image_id"]==8),doc["annotations"][1])
        self.assertEqual(next(a for a in saved["annotations"] if a["image_id"]==7)["bbox"],[32.,18.,128.,72.])

    def test_conversion_worker_reads_actual_image_sizes(self):
        import copy
        output=self.base/"converted"
        store=copy.deepcopy(self.app.label_store)
        snapshot=(store,list(self.paths),str(self.images),self.app._edit_path,copy.deepcopy(self.app._edit_shapes),output,"coco")
        self.app._annotation_export_worker(snapshot)
        messages=[]
        while not self.app.task_queue.empty():
            messages.append(self.app.task_queue.get_nowait())
        self.assertTrue(any(kind=="conversion_done" for kind,_ in messages))
        document=json.loads((output/"annotations.json").read_text())
        self.assertEqual(len(document["images"]),4)
        self.assertTrue(all((image["width"],image["height"])==(320,180) for image in document["images"]))


if __name__ == "__main__":
    unittest.main()
