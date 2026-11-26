"""Compact annotation review, bounded viewport rendering and task lifecycle."""
from __future__ import annotations
import copy
import json
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import ImageTk
from .annotation_formats import bounds
from .latest_worker import LatestWorker
from .recovery import DraftStore
from .review_support import shape_key, compare_shapes, viewport_spec, render_viewport


class ReviewUIMixin:
    def _init_review_ui(self):
        self._render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Viewport")
        self._viewport_pending = None
        self._viewport_future = None
        self._viewport_key = None
        self._viewport_requested = None
        self._interaction_after = None
        self._sharp_after = None
        self._review_signature = None
        self._comparison_worker = LatestWorker("AnnotationComparison")
        self._comparison_ticket = 0
        self._comparison_pending = False
        self._reviewed = set()
        self._background_futures = set()
        self._image_cache_lock = threading.Lock()
        self._draft_seen = set()
        self._draft_after = None
        self._heartbeat_time = time.monotonic()
        self._heartbeat_stop = threading.Event()
        self.root.report_callback_exception = self._report_ui_error
        self._heartbeat_after = self.root.after(500, self._heartbeat)
        self._watchdog_warned = False
        self._watchdog_visible = self.root.state() != "withdrawn"
        threading.Thread(target=self._watch_ui, daemon=True, name="UIWatchdog").start()
        self._edit_undo = deque(self._edit_undo, maxlen=30)

    def _cache_loaded_image(self, path, image):
        # A byte budget is essential: six 8K images still consume hundreds of MB.
        with self._image_cache_lock:
            self._image_cache[path] = image
            self._image_cache.move_to_end(path)
            budget = 256 * 1024 * 1024
            def size():
                return sum(im.width * im.height * len(im.getbands()) for im in self._image_cache.values())
            while len(self._image_cache) > 1 and (len(self._image_cache) > self._cache_max or size() > budget):
                self._image_cache.popitem(last=False)
        # Eviction drops ownership; workers/current viewport may still hold a reference.

    def _overlay_item(self, key, kind, coords, **options):
        pool = getattr(self,"_overlay_pool",None)
        if pool is None: self._overlay_pool=pool={}
        item = pool.get(key)
        if item is None:
            item = getattr(self.canvas,"create_"+kind)(*coords,**options)
            pool[key] = item
        else:
            self.canvas.coords(item,*coords)
            self.canvas.itemconfigure(item,**options)
        self._overlay_used.add(key)
        return item

    def _prune_overlay_items(self):
        pool=getattr(self,"_overlay_pool",{})
        for key in list(pool):
            if key not in self._overlay_used:
                self.canvas.delete(pool.pop(key))

    def _init_recovery(self, root):
        self._draft_store = DraftStore(root)

    def _draft_source(self):
        return self.coco_file_var.get() or self.label_dir_var.get()

    def _queue_recovery_draft(self):
        if not self._edit_dirty or not self._edit_path or not hasattr(self,"_draft_store"): return
        if self._edit_drag: return
        shapes = list(self._edit_shapes)+list(self._cur_extra_shapes)
        signature = (self._edit_path,self._draft_source(),repr(shapes))
        if signature == getattr(self,"_last_draft_signature",None): return
        self._last_draft_signature = signature
        self._draft_store.save(self._edit_path,self._draft_source(),shapes)

    def _offer_recovery_draft(self, path):
        if not hasattr(self,"_draft_store") or self._edit_dirty: return
        key = (path,self._draft_source())
        if key in self._draft_seen: return
        self._draft_seen.add(key)
        try:
            shapes = self._draft_store.load(*key)
        except (OSError,ValueError,TypeError,KeyError) as exc:
            self.notify_flow(f"[恢复] 草稿读取失败：{exc}");return
        if shapes is None: return
        answer=messagebox.askyesnocancel("恢复未保存标注", "发现此图片的未保存草稿。是否恢复？\n是：恢复到编辑区；否：删除草稿；取消：暂时保留草稿。",parent=self.root)
        if answer is None: return
        if not answer:
            self._draft_store.clear(*key);return
        self._push_edit_undo()
        for shape in shapes:
            shape.class_id=self.label_store.class_id_for_name(shape.class_name)
        self._edit_shapes=[s for s in shapes if s.kind in self.EDITABLE_KINDS]
        self._cur_extra_shapes=[s for s in shapes if s.kind not in self.EDITABLE_KINDS]
        self._edit_dirty=True
        self._sync_class_filter()
        self.render_image()
        self.notify_flow("[恢复] 草稿已恢复，请核对后保存")

    def _refresh_context_actions(self):
        focus = self._focus_shape
        prediction = focus is not None and any(focus is s for s in self._cur_pred_shapes)
        for attr,visible in (("_accept_prediction_button",prediction),
                             ("_confirm_review_button",focus is not None and self._needs_review(focus))):
            button=getattr(self,attr,None)
            if button:
                if visible and not button.winfo_manager(): button.pack(side="left",padx=4)
                elif not visible and button.winfo_manager(): button.pack_forget()
        cancel=getattr(self,"_cancel_scan_button",None)
        if cancel:
            visible=bool(getattr(self,"_scan_active",False) or getattr(self,"_label_scan_active",False) or getattr(self,"_import_active",False))
            if visible and not cancel.winfo_manager(): cancel.pack(side="right",padx=6)
            elif not visible and cancel.winfo_manager(): cancel.pack_forget()

    def _cancel_current_scan(self):
        self._dir_scan_token += 1
        self._label_scan_id += 1
        self._scan_active=self._label_scan_active=self._import_active=False
        self._refresh_context_actions()
        if getattr(self, "_import_cancel", None): self._import_cancel.set()
        self.set_busy("[取消] 已停止扫描 / 标签导入", 100)

    def _heartbeat(self):
        if self._closing: return
        if self._watchdog_warned:
            self._watchdog_warned = False
            self.notify_flow("[提示] 界面已恢复响应；如反复卡顿可查看运行日志")
        self._heartbeat_time = time.monotonic()
        self._queue_recovery_draft()
        if hasattr(self,"_draft_store") and self._draft_store.last_error:
            self.notify_flow(f"[恢复] 草稿保存失败：{self._draft_store.last_error}")
            self._draft_store.last_error = None
            self._last_draft_signature = None
        self._refresh_context_actions()
        self._heartbeat_after = self.root.after(500, self._heartbeat)

    def _watch_ui(self):
        while not self._heartbeat_stop.wait(1):
            if time.monotonic()-self._heartbeat_time > 8 and not self._watchdog_warned:
                self._watchdog_warned = True
                # No Tk calls from the monitoring thread.
                import logging
                logging.getLogger(__name__).warning("界面超过 8 秒未响应")
                if self._watchdog_visible:
                    self._show_unresponsive_notice()

    def _show_unresponsive_notice(self):
        # Independent native dialog still works when Tk's UI thread is blocked.
        import sys
        if sys.platform == "win32":
            def notice():
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(None,
                        "程序响应较慢，正在等待当前操作完成。\n请稍候，恢复后可取消扫描或导入。\n如持续无响应，可结束程序；未保存的修改可能丢失。",
                        "图片浏览器 · 响应提示", 0x40 | 0x1000)
                except Exception:
                    pass
            threading.Thread(target=notice, daemon=True, name="ResponseNotice").start()

    def _report_ui_error(self, exc_type, value, tb):
        import logging
        logging.getLogger(__name__).error("界面操作失败", exc_info=(exc_type,value,tb))
        self._show_task_error(str(value))

    def _show_task_error(self, message):
        if self._closing: return
        self.notify_flow(f"[错误] {message}")
        messagebox.showerror("操作未完成", f"{message}\n\n当前未保存标注仍保留，可重试或查看运行日志。", parent=self.root)

    def _push_edit_undo(self):
        if not isinstance(self._edit_undo, deque):
            self._edit_undo = deque(self._edit_undo, maxlen=30)
        self._edit_undo.append(self._edit_snapshot())

    def _schedule_interaction_render(self):
        if self._closing: return
        self._viewport_fast = True
        if self._interaction_after is None:
            self._interaction_after = self.root.after(16, self._interaction_render)
        if self._sharp_after is not None:
            self.root.after_cancel(self._sharp_after)
        self._sharp_after = self.root.after(150, self._sharp_render)

    def _interaction_render(self):
        self._interaction_after = None
        self.render_image()

    def _sharp_render(self):
        self._sharp_after = None
        self._viewport_fast = False
        self.render_image()

    def _request_viewport(self, path, original_size):
        if not hasattr(self, "_render_executor") or self._closing: return
        spec = viewport_spec(self.current_image.size, original_size, self.zoom,
                             (self.pan_x,self.pan_y),
                             (max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())))
        if spec is None:
            self._viewport_requested = None
            if self._canvas_image_id is not None:
                self.canvas.itemconfigure(self._canvas_image_id, state="hidden")
            return
        key = (path,id(self.current_image),spec,bool(getattr(self,"_viewport_fast",False)))
        self._viewport_requested = key
        if key == self._viewport_key:
            return
        request = (key, self.current_image, spec, bool(getattr(self,"_viewport_fast",False)))
        self._viewport_pending = request
        # Exactly one active job plus one replaceable latest request.
        if self._viewport_future is None:
            self._start_viewport_job()

    def _start_viewport_job(self):
        if self._closing or self._viewport_pending is None: return
        key,image,spec,fast = self._viewport_pending
        self._viewport_pending = None
        def work():
            try:
                display = render_viewport(image,spec,fast)
                if self._closing:
                    display.close()
                    return
                self.task_queue.put(("viewport_ready",(key,spec,display,None)))
            except Exception as exc:
                if not self._closing:
                    self.task_queue.put(("viewport_ready",(key,spec,None,str(exc))))
        self._viewport_future = self._render_executor.submit(work)

    def _apply_viewport_result(self, result):
        key,spec,display,error = result
        self._viewport_future = None
        if key == self._viewport_requested and not self._closing:
            if error:
                self.notify_flow(f"[图片] 渲染失败：{error}")
            else:
                self.tk_image = ImageTk.PhotoImage(display)
                x,y=spec[0]
                if self._canvas_image_id is None:
                    self._canvas_image_id=self.canvas.create_image(x,y,anchor="nw",image=self.tk_image)
                else:
                    self.canvas.coords(self._canvas_image_id,x,y)
                    self.canvas.itemconfigure(self._canvas_image_id,image=self.tk_image,state="normal")
                self.canvas.tag_lower(self._canvas_image_id)
                self._viewport_key=key
        if display is not None: display.close()
        if self._viewport_pending and self._viewport_pending[0] == self._viewport_key:
            self._viewport_pending = None
        self._start_viewport_job()

    def _update_review_comparison(self, gt, pred, path):
        signature=(path,self.label_dir_var.get(),self.pred_label_dir_var.get(),
                   tuple((id(s),shape_key(s)) for s in gt),tuple((id(s),shape_key(s)) for s in pred))
        if signature != self._review_signature:
            self._review_signature=signature
            self._reviewed.clear()
            self._comparison_ticket += 1
            ticket = self._comparison_ticket
            self._review_abnormal,self._review_pairs=set(),{}
            self._comparison_worker.invalidate()
            self._comparison_pending = False
            if self.pred_label_dir_var.get() and self._has_label_source():
                # Small comparisons stay immediate; expensive collections use snapshots.
                if len(gt)*len(pred) <= 64:
                    self._review_abnormal,self._review_pairs=compare_shapes(gt,pred)
                else:
                    self._comparison_pending = True
                    original_gt, original_pred = list(gt), list(pred)
                    snapshot_gt,snapshot_pred = copy.deepcopy(gt),copy.deepcopy(pred)
                    def compare(valid):
                        if not valid(): return
                        bad,pairs=compare_shapes(snapshot_gt,snapshot_pred)
                        mapping={id(a):b for a,b in zip(snapshot_gt+snapshot_pred,original_gt+original_pred)}
                        abnormal={id(mapping[key]) for key in bad}
                        mapped_pairs={id(mapping[key]):mapping[id(other)] for key,other in pairs.items()}
                        if valid() and not self._closing:
                            self.task_queue.put(("review_ready",(ticket,abnormal,mapped_pairs)))
                    self._comparison_worker.submit(compare)

    def _apply_review_result(self, payload):
        ticket,abnormal,pairs=payload
        if ticket != self._comparison_ticket: return
        self._review_abnormal,self._review_pairs=abnormal,pairs
        self._comparison_pending = False
        self.render_image()

    def _needs_review(self, shape):
        return id(shape) in getattr(self,"_review_abnormal",set()) and id(shape) not in self._reviewed

    def _confirm_review(self):
        shape=self._focus_shape
        if shape is None: return
        self._reviewed.add(id(shape))
        other=getattr(self,"_review_pairs",{}).get(id(shape))
        if other is not None: self._reviewed.add(id(other))
        self.render_image()

    def _accept_prediction(self):
        shape=self._focus_shape
        if shape is None or not any(shape is s for s in self._cur_pred_shapes):
            self.notify_flow("[标注] 请先选择一个预测框")
            return
        # A configured output source prevents silently writing into prediction data.
        if not self._has_label_source():
            self.notify_flow("[标注] 请先导入人工标签目录作为保存位置")
            self.show_label_import()
            return
        self._push_edit_undo()
        accepted=copy.deepcopy(shape)
        accepted.class_id=self.label_store.class_id_for_name(shape.class_name)
        accepted.confidence=None
        accepted.metadata={k:v for k,v in accepted.metadata.items()
                           if k not in {"coco_annotation","score","id"}}
        self._edit_shapes.append(accepted)
        self._edit_sel=self._focus_shape=accepted
        self._edit_dirty=True
        self._sync_class_filter()
        self.render_image()

    def _pick_canvas_shape(self, x, y):
        import cv2
        import numpy as np
        hits=self.canvas.find_overlapping(x-4,y-4,x+4,y+4)
        candidates=[]
        for item in reversed(hits):
            shape=self._shape_item_map.get(item)
            if shape is not None and not any(shape is s for s in candidates): candidates.append(shape)
        for source,enabled in ((self._edit_shapes,self.show_gt_var.get() and not self._tab_pred_only),
                               (self._cur_pred_shapes,self.show_pred_var.get() or self._tab_pred_only)):
            if not enabled or not self.labels_visible.get(): continue
            for shape in reversed(source):
                if self.label_store.active_classes is not None and shape.class_id not in self.label_store.active_classes: continue
                if len(shape.points)<3 or any(shape is s for s in candidates): continue
                polygon=np.asarray([self._editor_to_canvas(p) for p in shape.points],np.float32)
                if cv2.pointPolygonTest(polygon,(float(x),float(y)),False)>=0: candidates.append(shape)
        previous=getattr(self,"_last_pick",None)
        index=0
        if previous and abs(previous[0]-x)<5 and abs(previous[1]-y)<5:
            current=next((i for i,s in enumerate(candidates) if s is self._focus_shape),-1)
            index=(current+1)%len(candidates) if candidates else 0
        self._last_pick=(x,y)
        return candidates[index] if candidates else None

    def _center_on_shape(self, shape):
        if not self.current_image: return
        x,y,r,b=bounds(shape.points)
        path=self.image_paths[self.current_index]
        w,h=self._original_sizes.get(path,self.current_image.size)
        self.pan_x=self.canvas.winfo_width()/2-(x+r)/2*w*self.zoom
        self.pan_y=self.canvas.winfo_height()/2-(y+b)/2*h*self.zoom

    def _reset_review_dataset(self):
        self._review_signature=None
        self._comparison_ticket += 1
        self._comparison_worker.invalidate()
        self._reviewed.clear()
        self._draw_class_id=None
        self._viewport_requested=None
        self._viewport_key=None
        self._viewport_pending=None
        self._edit_undo=deque(maxlen=30)
        self._edit_sel=self._focus_shape=None
        self._edit_dirty=False
        self._edit_path=""
        for var in self.class_vars.values(): var.set(True)
        self.label_store.active_classes=None
        self._label_cache.clear()
        self.label_store._label_cache.clear()
        self.pred_store._label_cache.clear()

    def _release_review_resources(self):
        self._heartbeat_stop.set()
        self._comparison_worker.close()
        if hasattr(self, "_draft_store"): self._draft_store.close()
        self._viewport_requested=None
        self._viewport_pending=None
        for name in ("_interaction_after","_sharp_after","_heartbeat_after"):
            value=getattr(self,name,None)
            if value:
                try: self.root.after_cancel(value)
                except tk.TclError: pass
        for future in list(self._background_futures): future.cancel()
        self._render_executor.shutdown(wait=False,cancel_futures=True)
        self._edit_undo.clear()
        self._edit_shapes.clear()
        self._cur_pred_shapes.clear()
        self._reviewed.clear()
        self.label_store._label_cache.clear()
        self.pred_store._label_cache.clear()
        if getattr(self,"_import_cancel",None): self._import_cancel.set()
        self._viewport_key=None
        self.label_store._labelme_documents.clear()
        self.pred_store._labelme_documents.clear()
        self.thumb_refs.clear()
        self.dedupe_groups.clear()
        self._dedupe_task_states.clear()
        self._workspace_browse_state = None
        self.current_image=None
        self.tk_image=None
        while not self.task_queue.empty():
            try:
                kind,payload=self.task_queue.get_nowait()
                if kind=="viewport_ready" and payload[2] is not None: payload[2].close()
            except Exception: break

    def show_label_import(self):
        dialog=tk.Toplevel(self.root)
        dialog.title("导入标签")
        dialog.transient(self.root)
        dialog.grab_set()
        frame=ttk.Frame(dialog,padding=16)
        frame.pack(fill="both",expand=True)
        source=tk.StringVar(value="人工标注")
        location=tk.StringVar()
        class_file=tk.StringVar(value=self.classes_file_var.get())
        ttk.Label(frame,text="标签来源").grid(row=0,column=0,sticky="w",pady=6)
        ttk.Combobox(frame,textvariable=source,values=["人工标注","预测结果"],state="readonly",width=18).grid(row=0,column=1,sticky="w")
        ttk.Label(frame,text="标签位置").grid(row=1,column=0,sticky="w",pady=6)
        ttk.Entry(frame,textvariable=location,width=48).grid(row=1,column=1,columnspan=2,sticky="ew")
        def choose_folder():
            path=filedialog.askdirectory(parent=dialog,title="选择标签目录")
            if path:
                location.set(path)
                candidate=Path(path)/"classes.txt"
                if candidate.is_file(): class_file.set(str(candidate))
        def choose_json():
            path=filedialog.askopenfilename(parent=dialog,title="选择 COCO / LabelMe JSON",filetypes=[("JSON","*.json")])
            if path: location.set(path)
        buttons=ttk.Frame(frame);buttons.grid(row=2,column=1,columnspan=2,sticky="e",pady=5)
        ttk.Button(buttons,text="选择目录",command=choose_folder).pack(side="left",padx=4)
        ttk.Button(buttons,text="选择 JSON",command=choose_json).pack(side="left")
        ttk.Label(frame,text="类别表").grid(row=3,column=0,sticky="w",pady=6)
        choices=["使用当前类别表"] if self.label_store.classes else []
        combo=ttk.Combobox(frame,textvariable=class_file,values=choices+list(self._classes_file_history),width=40)
        combo.grid(row=3,column=1,sticky="ew")
        if self.label_store.classes and not class_file.get(): class_file.set("使用当前类别表")
        def choose_classes():
            path=filedialog.askopenfilename(parent=dialog,title="选择类别文件",filetypes=[("类别文本","*.txt")])
            if path: class_file.set(path)
        ttk.Button(frame,text="导入类别…",command=choose_classes).grid(row=3,column=2,padx=(6,0))
        preview=tk.StringVar()
        def update_preview(*_):
            selection=class_file.get()
            try:
                names=self.label_store.classes if selection=="使用当前类别表" else Path(selection).read_text(encoding="utf-8-sig").splitlines() if selection else []
                preview.set(f"{len(names)} 个类别："+"；".join(f"{i}={n}" for i,n in enumerate(names[:5])))
            except OSError: preview.set("请选择有效类别文件，或使用当前类别表")
        class_file.trace_add("write",update_preview);update_preview()
        ttk.Label(frame,textvariable=preview,wraplength=460,style="Hint.TLabel").grid(row=4,column=0,columnspan=3,sticky="w",pady=6)
        ttk.Label(frame,text="YOLO 需要类别表；LabelMe / COCO 自动读取类别名称。",style="Hint.TLabel").grid(row=5,column=0,columnspan=3,sticky="w")
        status=tk.StringVar()
        ttk.Label(frame,textvariable=status,foreground="#dc2626",wraplength=460).grid(row=6,column=0,columnspan=3,sticky="w",pady=6)
        def start():
            path=Path(location.get())
            if not location.get() or not path.exists():
                status.set("请选择有效标签目录或 JSON 文件");return
            selection=class_file.get()
            names=list(self.label_store.classes) if selection=="使用当前类别表" else []
            if selection and selection!="使用当前类别表":
                try: names=[n.strip() for n in Path(selection).read_text(encoding="utf-8-sig").splitlines() if n.strip()]
                except (OSError,UnicodeError) as exc: status.set(str(exc));return
            # Validate YOLO ids in background, allowing cancellation without mutating state.
            cancelled=threading.Event()
            self._import_cancel=cancelled
            self._import_active=True
            self._refresh_context_actions()
            status.set("正在检查标签，可取消…")
            apply_btn.configure(state="disabled")
            def validate():
                try:
                    count=0
                    has_yolo=False
                    if path.is_dir():
                        for file in path.rglob("*.txt"):
                            if cancelled.is_set() or self._closing: return
                            if file.name.lower() in {"classes.txt","classes.names","labels.txt"} or str(file)==selection: continue
                            for line in file.read_text(encoding="utf-8-sig").splitlines():
                                if not line.strip(): continue
                                parts=line.split()
                                try:
                                    cid=int(parts[0]);coordinates=[float(v) for v in parts[1:]]
                                except ValueError:
                                    raise ValueError(f"{file.name} 包含无效标签行")
                                has_yolo=True;count+=1
                                if len(coordinates)<4: raise ValueError(f"{file.name} 坐标数量不足")
                                if not names: raise ValueError("YOLO 标签缺少类别表，请在此窗口选择或导入类别文件")
                                if cid<0 or cid>=len(names): raise ValueError(f"{file.name} 的类别 ID {cid} 超出类别表范围（{len(names)} 类）")
                    if source_value=="预测结果" and names and self.label_store.classes and names!=self.label_store.classes:
                        raise ValueError("预测类别表与人工类别表不一致，请先统一类别 ID 与名称对应关系")
                    self.task_queue.put(("label_import_checked",(dialog,path,source_value,selection,names,has_yolo,count,cancelled,status,apply_btn,None)))
                except Exception as exc:
                    self.task_queue.put(("label_import_checked",(dialog,path,source_value,selection,names,False,0,cancelled,status,apply_btn,str(exc))))
            source_value=source.get()
            self._run_thread(validate)
        def cancel():
            self._import_active=False
            self._refresh_context_actions()
            if getattr(self,"_import_cancel",None): self._import_cancel.set()
            dialog.destroy()
        bottom=ttk.Frame(frame);bottom.grid(row=7,column=0,columnspan=3,sticky="e",pady=(8,0))
        ttk.Button(bottom,text="取消",command=cancel).pack(side="left",padx=6)
        apply_btn=ttk.Button(bottom,text="导入",command=start);apply_btn.pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW",cancel)
        dialog.bind("<Escape>",lambda _:cancel())

    def _finish_label_import(self, payload):
        dialog,path,source,selection,names,has_yolo,count,cancelled,status,button,error=payload
        if cancelled.is_set() or self._closing or not dialog.winfo_exists(): return
        self._import_active=False
        self._refresh_context_actions()
        button.configure(state="normal")
        if error:
            status.set(error);return
        if not self._resolve_pending_edits(): return
        if names:
            self.label_store.classes=list(names)
            if selection!="使用当前类别表":
                self.classes_file_var.set(selection)
                self._add_classes_file_history(selection)
        if source=="人工标注":
            if not self._activate_annotation_source(path, use_selected_classes=bool(names)): return
        else:
            try:
                if path.is_file():
                    data=json.loads(path.read_text(encoding="utf-8-sig"))
                    if all(k in data for k in ("images","annotations","categories")):
                        self.pred_store.load_coco(str(path))
                        self.pred_label_dir_var.set(str(path))
                    elif isinstance(data.get("shapes"),list):
                        self.pred_label_dir_var.set(str(path.parent))
                    else: raise ValueError("请选择有效 COCO / LabelMe JSON")
                else: self.pred_label_dir_var.set(str(path))
                self._apply_pred_dir_selection()
                self._sync_class_filter()
            except (ValueError,OSError) as exc:
                status.set(str(exc));return
        for var in self.class_vars.values(): var.set(True)
        self._on_class_filter_changed(render=False)
        self._review_signature=None
        self.render_image()
        self._save_config()
        dialog.destroy()
        self.notify_flow(f"[标签] 已导入{source}"+(f"，检查 {count} 条 YOLO 标注" if has_yolo else ""))
