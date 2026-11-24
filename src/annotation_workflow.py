"""标签导入与批量转换的统一入口。"""
import copy
import json
import time
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image

try:
    from .annotation_formats import AnnotationError, export_dataset
except ImportError:
    from annotation_formats import AnnotationError, export_dataset


class AnnotationWorkflowMixin:
    EXPORT_FORMATS = {
        "LabelMe / X-AnyLabeling JSON": "labelme",
        "COCO JSON（检测 / 实例分割）": "coco",
        "YOLO 水平框 HBB": "yolo_hbb",
        "YOLO 旋转框 OBB": "yolo_obb",
        "YOLO 多边形分割": "yolo_segment",
    }

    def _build_annotation_inspector(self):
        panel = self._register_panel("labels")
        tab, bar = panel.body, panel.action_bar
        layers = ttk.Frame(tab)
        layers.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(layers, text="人工标签 GT", variable=self.show_gt_var, command=self._on_display_option_changed).pack(side="left")
        ttk.Checkbutton(layers, text="预测 PRED", variable=self.show_pred_var, command=self._on_display_option_changed).pack(side="left", padx=6)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="绘制类别").pack(side="left", padx=(0, 8))
        # 可输入状态下拉：既可下拉选择「序号·名称」，也可直接输入序号定位类别（回车/失焦生效）
        self._draw_class_combo = ttk.Combobox(row, state="normal", width=12)
        self._draw_class_combo.pack(side="left", fill="x", expand=True)
        self._draw_class_combo.bind("<<ComboboxSelected>>", self._on_draw_class_selected)
        self._draw_class_combo.bind("<Return>", self._on_draw_class_selected)
        self._draw_class_combo.bind("<FocusOut>", self._on_draw_class_selected)
        self._icon_button(row, "plus", self._editor_add_class, "添加标注类别").pack(side="left", padx=(4, 0))
        ttk.Label(tab, text="当前图片的标注", style="Title.TLabel").pack(anchor="w", pady=(4, 5))
        listing = ttk.Frame(tab)
        listing.pack(fill="x")
        self._object_tree = ttk.Treeview(listing, columns=("class", "kind", "points"), show="headings", height=6, selectmode="browse")
        for column, title, width in [("class", "类别", 140), ("kind", "类型", 60), ("points", "顶点", 45)]:
            self._object_tree.heading(column, text=title)
            self._object_tree.column(column, width=width, minwidth=35)
        scroll = ttk.Scrollbar(listing, command=self._object_tree.yview)
        scroll.pack(side="right", fill="y")
        self._object_tree.configure(yscrollcommand=scroll.set)
        self._object_tree.pack(fill="x", expand=True)
        self._object_tree.bind("<<TreeviewSelect>>", self._editor_select_object)
        self._object_tree.bind("<Double-1>", self._editor_edit_class)
        self._object_tree.bind("<Button-3>", self._editor_tree_menu)
        # 类别过滤内嵌到标注区内：✓ 芯片点击切换显示
        self._add_class_filter_panel(tab, expand=False)
        ttk.Label(tab, text="单击定位 · 双击改类别 · 右键改类别 / 删除 · Delete 删除", style="Hint.TLabel").pack(anchor="w", pady=5)
        edit_actions = ttk.Frame(tab)
        edit_actions.pack(fill="x", pady=(0, 10))
        self._icon_button(edit_actions, "tag", self._editor_edit_class, "修改选中标注的类别").pack(side="left")
        self._icon_button(edit_actions, "trash", self._edit_delete_selected, "删除选中标注 · Delete").pack(side="left", padx=4)

        sources = ttk.Frame(tab)
        toggle = ttk.Button(tab, text="来源与格式设置 ▸", image=self._icons.get("settings",size=18), compound="left")
        toggle.pack(fill="x", pady=(10, 4))
        def toggle_sources():
            if sources.winfo_manager():
                sources.pack_forget()
                toggle.configure(text="来源与格式设置 ▸")
            else:
                sources.pack(fill="x", after=toggle)
                toggle.configure(text="来源与格式设置 ▾")
        toggle.configure(command=toggle_sources)
        self._label_sources_frame = sources
        self._label_dir_label = ttk.Label(sources, text="", wraplength=280, style="Hint.TLabel")
        self._label_dir_label.pack(fill="x")
        for title, variable, history, attribute, apply, choose in [
            ("人工标签目录", self.label_dir_var, self._label_dir_history, "_label_dir_combo", self._apply_label_dir_selection, self.select_label_dir),
            ("预测目录（可选）", self.pred_label_dir_var, self._pred_dir_history, "_pred_dir_combo", self._apply_pred_dir_selection, self.select_pred_label_dir),
        ]:
            ttk.Label(sources, text=title).pack(anchor="w", pady=(6, 2))
            row = ttk.Frame(sources)
            row.pack(fill="x")
            combo = ttk.Combobox(row, textvariable=variable, values=history, width=16)
            setattr(self, attribute, combo)
            combo.pack(side="left", fill="x", expand=True)
            combo.bind("<Return>", lambda _e, f=apply: f())
            combo.bind("<<ComboboxSelected>>", lambda _e, f=apply: f())
            self._icon_button(row, "folder", choose, "选择标注目录").pack(side="left", padx=(4, 0))
        ttk.Button(sources, text="清除预测目录", command=self._clear_pred_dir).pack(anchor="e", pady=4)
        ttk.Label(sources, text="类别文件（可选）").pack(anchor="w")
        row = ttk.Frame(sources)
        row.pack(fill="x", pady=4)
        self.classes_combo = ttk.Combobox(row, textvariable=self.classes_file_var, values=self._classes_file_history, width=16)
        self.classes_combo.pack(side="left", fill="x", expand=True)
        self.classes_combo.bind("<<ComboboxSelected>>", self._on_classes_combo_selected)
        self.classes_combo.bind("<Return>", self._on_classes_combo_selected)
        self._icon_button(row, "folder", self.select_classes, "选择 classes.txt").pack(side="left", padx=4)
        self._coco_file_label = ttk.Label(sources, text="", wraplength=280, style="Hint.TLabel")
        self._coco_file_label.pack(fill="x")
        self._detected_format_var = tk.StringVar()
        ttk.Label(sources, textvariable=self._detected_format_var, style="Hint.TLabel").pack(fill="x", pady=4)
        self.label_auto_var = tk.BooleanVar(value=True)
        self._manual_label_frame = ttk.Frame(sources)
        def on_auto():
            if self.label_auto_var.get():
                self._manual_label_frame.pack_forget()
                self.label_format_var.set("auto")
                self.yolo_type_var.set("auto")
                self.detect_box_format_var.set("auto")
            else:
                self._manual_label_frame.pack(fill="x")
            self.on_label_setting_changed()
        ttk.Checkbutton(sources, text="自动识别格式", variable=self.label_auto_var, command=on_auto).pack(anchor="w")
        for title, variable, values in [("数据格式", self.label_format_var, ["auto", "yolo", "labelme", "coco"]),
                                        ("YOLO 任务", self.yolo_type_var, ["auto", "detect", "obb", "segment"]),
                                        ("HBB 坐标", self.detect_box_format_var, ["auto", "xywh", "xyxy"])]:
            ttk.Label(self._manual_label_frame, text=title).pack(anchor="w")
            combo = ttk.Combobox(self._manual_label_frame, textvariable=variable, values=values, state="readonly")
            combo.pack(fill="x", pady=3)
            combo.bind("<<ComboboxSelected>>", lambda _e: self.on_label_setting_changed())
        ttk.Label(sources, text="四点 YOLO polygon 与 OBB 无法仅凭文本区分；请指定 YOLO 任务。", wraplength=280, style="Hint.TLabel").pack(fill="x", pady=6)
        self.label_progress_frame = ttk.Frame(sources)
        self.label_progress_var = tk.DoubleVar(value=0)
        self.label_progress_bar = ttk.Progressbar(self.label_progress_frame, variable=self.label_progress_var, maximum=100)
        self.label_progress_bar.pack(side="left", fill="x", expand=True)
        self.label_progress_text = ttk.Label(self.label_progress_frame, text="0/0")
        self.label_progress_text.pack(side="right")
        self._label_tree_frame = ttk.Frame(sources)
        self._label_tree_frame.pack(fill="x")
        self.label_tree = ttk.Treeview(self._label_tree_frame, columns=("status", "image", "label"), show="headings", height=3)
        for column, title, width in [("status", "匹配", 40), ("image", "图片", 115), ("label", "标签", 110)]:
            self.label_tree.heading(column, text=title)
            self.label_tree.column(column, width=width, minwidth=30)
        self.label_tree.pack(fill="x")
        self.label_tree.bind("<Double-1>", self._on_label_tree_open)
        self.label_tree.tag_configure("current", background="#dceaff", foreground="#164aa3")

        # 保存（Ctrl+S / 自动保存常开）、导入、导出统一走菜单，避免重复入口；操作区只保留撤销
        self._icon_button(bar, "undo", self._edit_undo_pop, "撤销 · Ctrl+Z", text="撤销", style="TButton").pack(fill="x")

    def import_annotation_file(self):
        path = filedialog.askopenfilename(title="导入 LabelMe / X-AnyLabeling / COCO JSON", filetypes=[("JSON 标注", "*.json")])
        if path:
            self._activate_annotation_source(Path(path))

    def _activate_annotation_source(self, path):
        if not self._resolve_pending_edits():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else None
            if data and all(k in data for k in ("images", "annotations", "categories")):
                self.label_store.load_coco(str(path))
                self.coco_file_var.set(str(path))
                self.label_format_var.set("coco")
            elif path.is_dir() or data is not None and isinstance(data.get("shapes"), list):
                folder = path if path.is_dir() else path.parent
                self.label_format_var.set("labelme" if path.is_file() else "auto")
                self.coco_file_var.set("")
                self.label_store.coco_file = ""
                self.label_store._coco_document = {}
                self.label_dir_var.set(str(folder))
                self.yolo_type_var.set("auto")
                classes = folder / "classes.txt"
                if classes.exists():
                    self._load_classes_from_path(str(classes), render=False)
                self._apply_label_dir_selection()
            else:
                raise AnnotationError("请选择 COCO JSON、LabelMe JSON 或标注目录")
            self.label_store.clear_label_cache()
            self._label_cache.clear()
            self._sync_class_filter()
            self.show_panel("labels")
            self.refresh_current_labels(update_list=True)
            self._save_config()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)

    def show_annotation_export(self, scope="全部图片"):
        if not self.image_paths:
            self.notify_flow("[转换] 请先打开图片和标注")
            return
        if getattr(self, "_conversion_running", False):
            self.notify_flow("[转换] 已有转换任务正在执行")
            return
        if getattr(self, "_image_loading", False):
            self.notify_flow("[转换] 当前图片正在加载，请稍后导出")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("导出 / 转换标注")
        dialog.geometry("520x430")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="导出 / 转换标注", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"当前数据：{len(self.image_paths)} 张图片 · 不修改原始标注", style="Hint.TLabel").pack(anchor="w", pady=(5, 16))
        scope_var = tk.StringVar(value=scope)
        ttk.Label(body, text="导出范围").pack(anchor="w")
        ttk.Combobox(body, textvariable=scope_var, values=["当前图片", "全部图片"], state="readonly").pack(fill="x", pady=(4, 12))
        format_var = tk.StringVar(value=next(iter(self.EXPORT_FORMATS)))
        ttk.Label(body, text="目标格式 / 训练任务").pack(anchor="w")
        combo = ttk.Combobox(body, textvariable=format_var, values=list(self.EXPORT_FORMATS), state="readonly")
        combo.pack(fill="x", pady=(4, 12))
        note = tk.StringVar()
        ttk.Label(body, textvariable=note, wraplength=450, justify="left", style="Hint.TLabel").pack(fill="x")
        def update_note(_event=None):
            target = self.EXPORT_FORMATS[format_var.get()]
            note.set({"labelme": "一图一 JSON，可混合 HBB、OBB 和多边形，保留 flags、group_id 等信息。",
                      "coco": "所有图片汇总为一个 JSON；多部件实例保留分组，OBB 按四角多边形保存。"}.get(target,
                     "将所有目标转换为所选训练任务。外接框转换会减少几何细节；YOLO 不保存 flags、group_id 等字段。") + "\n\n自动换算像素 / 归一化坐标及类别 ID。输出到新建子目录，保留图片相对层级，并附转换报告。")
        combo.bind("<<ComboboxSelected>>", update_note)
        update_note()
        actions = ttk.Frame(body)
        actions.pack(side="bottom", fill="x")
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="right", padx=(8, 0))
        def start():
            parent = filedialog.askdirectory(title="选择导出位置（自动创建新子目录）", parent=dialog)
            if not parent:
                return
            target = self.EXPORT_FORMATS[format_var.get()]
            output = Path(parent) / f"annotations_{target}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
            paths = [self.image_paths[self.current_index]] if scope_var.get() == "当前图片" else list(self.image_paths)
            # 主线程快照：后台转换不读取 Tk 变量，不受切图、切换目录影响。
            store = copy.deepcopy(self.label_store)
            store.active_classes = None
            store._label_cache.clear()
            store.format = self.label_format_var.get()
            store.yolo_type = self.yolo_type_var.get()
            store.detect_box_format = self.detect_box_format_var.get()
            current_shapes = copy.deepcopy(self._edit_shapes + self._cur_extra_shapes)
            if not self.pred_label_dir_var.get():
                current_shapes += copy.deepcopy(self._cur_pred_shapes)
            snapshot = (store, paths, self.dir_var.get(), self._edit_path, current_shapes, output, target)
            self._conversion_running = True
            self.notify_flow("[转换] 正在读取图片尺寸与标注…", 0)
            dialog.destroy()
            self._run_thread(self._annotation_export_worker, snapshot)
        ttk.Button(actions, text="选择位置并导出…", style="Action.TButton", command=start).pack(side="right")

    def _annotation_export_worker(self, snapshot):
        store, paths, root, edit_path, current_shapes, output, target = snapshot
        try:
            records = []
            for index, path in enumerate(paths):
                if self._closing:
                    return
                with Image.open(path) as image:
                    size = image.size
                shapes = current_shapes if path == edit_path else store.load_for_image(path, size, include_hidden=True)
                try:
                    relative = Path(path).relative_to(root).as_posix()
                except ValueError:
                    relative = Path(path).name
                records.append({"image_path": path, "file_name": relative, "size": size, "shapes": shapes,
                                "labelme_document": store._labelme_documents.get(path)})
                if index % 50 == 0:
                    self.task_queue.put(("conversion_progress", (index + 1, len(paths))))
            report = export_dataset(records, store.classes, output, target)
            self.task_queue.put(("conversion_done", (output, target, report)))
        except Exception as exc:
            self.task_queue.put(("conversion_error", str(exc)))

    def _show_conversion_result(self, output, target, report):
        self._conversion_running = False
        self.notify_flow(f"[转换] 完成：{report['images']} 张图片，{report['shapes']} 个标注 → {output}", 100)
        dialog = tk.Toplevel(self.root)
        dialog.title("标注转换完成")
        dialog.transient(self.root)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"已导出 {report['images']} 张图片的标注", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=str(output), wraplength=480).pack(anchor="w", pady=12)
        for warning in report["warnings"]:
            ttk.Label(body, text="• " + warning, wraplength=480, style="Hint.TLabel").pack(anchor="w", pady=3)
        ttk.Label(body, text="详细映射与转换说明见 conversion_report.json", style="Hint.TLabel").pack(anchor="w", pady=10)
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(8, 0))
        def load_result():
            dialog.destroy()
            self._activate_annotation_source(output / "annotations.json" if target == "coco" else output)
        ttk.Button(row, text="加载转换结果", command=load_result, style="Action.TButton").pack(side="right")
        ttk.Button(row, text="关闭", command=dialog.destroy).pack(side="right", padx=8)
