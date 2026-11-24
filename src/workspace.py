"""图片工作台的布局和交互；文件读写、标签编辑仍由应用负责。"""
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox


class WorkspaceMixin:
    IMAGE_SECTIONS = {"browse", "labels", "marks"}
    LIBRARY_PAGE_SIZE = 200

    def _build_library_panel(self, parent):
        """构建图片库区（搜索 + 列表 + 分页 + 最近目录），挂到传入的父容器。

        打开目录的唯一主入口在顶部工具栏（菜单 / Ctrl+O / 空状态引导同路径），
        此处只保留「最近打开」快捷下拉，不再重复放置打开按钮与含子目录开关。
        """
        self._library_pane = ttk.Frame(parent, padding=(8, 8))
        pane = self._library_pane
        ttk.Label(pane, text="图片库", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(pane, text="当前文件夹 / 最近打开（回车加载）", style="Hint.TLabel").pack(anchor="w")
        self._dir_combo = ttk.Combobox(pane, textvariable=self.dir_var, values=self._dir_history, width=30)
        self._dir_combo.pack(fill="x", pady=(4, 8))
        self._dir_combo.bind("<Return>", lambda _e: self.load_image_dir(self.dir_var.get()))
        self._dir_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_image_dir(self.dir_var.get()))
        ttk.Separator(pane).pack(fill="x", pady=(0, 8))
        self._image_search_var = tk.StringVar()
        row = ttk.Frame(pane)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, image=self._icons.get("search", size=18)).pack(side="left", padx=(0, 5))
        self._image_search_entry = ttk.Entry(row, textvariable=self._image_search_var)
        self._image_search_entry.pack(side="left", fill="x", expand=True)
        self._icon_button(row, "close", lambda: self._image_search_var.set(""), "清空搜索").pack(side="left", padx=(3, 0))
        self._image_search_var.trace_add("write", self._schedule_library_filter)
        self._image_search_entry.bind("<Escape>", lambda _e: self._image_search_var.set(""))
        self._image_search_entry.bind("<Return>", self._open_first_search_result)
        self._library_count_var = tk.StringVar(value="打开文件夹后，图片会显示在这里")
        ttk.Label(pane, textvariable=self._library_count_var, style="Hint.TLabel", wraplength=320).pack(fill="x", pady=(0, 6))
        listing = ttk.Frame(pane)
        listing.pack(fill="both", expand=True)
        self._library_tree = ttk.Treeview(listing, columns=("name",), show="headings", selectmode="browse", height=7)
        self._library_tree.heading("name", text="图片名称")
        self._library_tree.column("name", width=280, minwidth=120)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self._library_tree.yview)
        scroll.pack(side="right", fill="y")
        self._library_tree.configure(yscrollcommand=scroll.set)
        self._library_tree.pack(fill="both", expand=True)
        self._library_tree.bind("<<TreeviewSelect>>", self._on_library_select)
        self._library_tree.bind("<Return>", lambda _e: self.canvas.focus_set())
        paging = ttk.Frame(pane)
        paging.pack(fill="x", pady=(6, 0))
        self._library_prev = self._icon_button(paging, "previous", lambda: self._turn_library_page(-1), "上一页")
        self._library_prev.pack(side="left")
        self._library_page_var = tk.StringVar(value="0 / 0")
        ttk.Label(paging, textvariable=self._library_page_var, anchor="center").pack(side="left", fill="x", expand=True)
        self._library_next = self._icon_button(paging, "next", lambda: self._turn_library_page(1), "下一页")
        self._library_next.pack(side="right")
        self._library_indices = []
        self._library_positions = {}
        self._library_signature = None
        self._library_page = 0
        self._library_filter_after = None
        self._library_rendered_page = None

    def _build_empty_state(self):
        self._empty_state = ttk.Frame(self.image_canvas_host, padding=28)
        ttk.Label(self._empty_state, text="从一个图片文件夹开始", font=("Microsoft YaHei UI", 18, "bold")).pack(pady=(0, 12))
        ttk.Label(self._empty_state, text="打开即预览，自动发现同级 labels 标签。", foreground="#64748b").pack()
        self._icon_button(self._empty_state, "folder", self.open_image_dir, "打开文件夹 · Ctrl+O", text="打开图片文件夹…", style="Action.TButton").pack(pady=20)
        if self.config.get("last_dir"):
            ttk.Button(self._empty_state, text="继续上次浏览", command=self._resume_workspace).pack(pady=(0, 12))
        ttk.Label(self._empty_state, text="1  选择图片    →    2  检查标签    →    3  分类整理", foreground="#526176").pack()
        ttk.Label(self._empty_state, text="Ctrl+O 打开文件夹    ·    Ctrl+F 搜索图片", style="Hint.TLabel").pack(pady=(16, 0))
        self._empty_state.place(relx=.5, rely=.5, anchor="center")

    def _refresh_workspace(self, rebuild=False):
        if not hasattr(self, "_library_tree"):
            return
        signature = (id(self.image_paths), len(self.image_paths))
        if rebuild or signature != self._library_signature:
            self._library_signature = signature
            query = self._image_search_var.get().strip().casefold()
            root = self.dir_var.get()
            self._library_names = []
            for path in self.image_paths:
                try:
                    name = str(Path(path).relative_to(root)) if root else Path(path).name
                except ValueError:
                    name = Path(path).name
                self._library_names.append(name)
            self._library_indices = [i for i, name in enumerate(self._library_names) if query in name.casefold()]
            self._library_positions = {idx: pos for pos, idx in enumerate(self._library_indices)}
            pos = self._library_positions.get(self.current_index, 0)
            self._library_page = pos // self.LIBRARY_PAGE_SIZE
            self._library_rendered_page = None
        self._render_library_page()
        has_images = bool(self.image_paths)
        self._empty_state.place_forget() if has_images else self._empty_state.place(relx=.5, rely=.5, anchor="center")
        if not has_images:
            self._index_entry.delete(0, "end")
            self._index_entry.insert(0, "0")
            self._total_label.configure(text=" / 0")
        self._update_dedupe_task_buttons()

    def _render_library_page(self, follow_current=False):
        if follow_current and self.current_index in self._library_positions:
            self._library_page = self._library_positions[self.current_index] // self.LIBRARY_PAGE_SIZE
        count = len(self._library_indices)
        pages = (count + self.LIBRARY_PAGE_SIZE - 1) // self.LIBRARY_PAGE_SIZE
        self._library_page = min(self._library_page, max(0, pages - 1))
        start = self._library_page * self.LIBRARY_PAGE_SIZE
        if self._library_rendered_page != self._library_page:
            self._library_tree.delete(*self._library_tree.get_children())
            for idx in self._library_indices[start:start + self.LIBRARY_PAGE_SIZE]:
                self._library_tree.insert("", "end", iid=str(idx), values=(self._library_names[idx],))
            self._library_rendered_page = self._library_page
        self._library_count_var.set(f"{count} 张匹配 / 共 {len(self.image_paths)} 张" if self._image_search_var.get().strip() else f"共 {count} 张图片")
        self._library_page_var.set(f"{self._library_page + 1 if pages else 0} / {pages}")
        self._library_prev.state(["!disabled"] if self._library_page > 0 else ["disabled"])
        self._library_next.state(["!disabled"] if self._library_page + 1 < pages else ["disabled"])
        self._library_tree.heading("name", text="没有匹配图片，请修改搜索词" if not count and self._image_search_var.get().strip() else "图片名称")
        iid = str(self.current_index)
        if self._library_tree.exists(iid):
            if self._library_tree.selection() != (iid,):
                self._library_tree.selection_set(iid)
                self._library_tree.focus(iid)
                self._library_tree.see(iid)

    def _schedule_library_filter(self, *_args):
        if self._library_filter_after:
            self.root.after_cancel(self._library_filter_after)
        self._library_filter_after = self.root.after(160, self._apply_library_filter)

    def _apply_library_filter(self):
        if self._library_filter_after:
            self.root.after_cancel(self._library_filter_after)
        self._library_filter_after = None
        self._refresh_workspace(rebuild=True)

    def _on_library_select(self, _event=None):
        selection = self._library_tree.selection()
        if not selection:
            return
        idx = int(selection[0])
        if idx != self.current_index and 0 <= idx < len(self.image_paths):
            self._cancel_pending_load()
            self.current_index = idx
            self.show_current_image()

    def _open_first_search_result(self, _event=None):
        self._apply_library_filter()
        if self._library_indices:
            self.current_index = self._library_indices[0]
            self.show_current_image()
            self.canvas.focus_set()
        return "break"

    def _turn_library_page(self, delta):
        self._library_page += delta
        self._render_library_page()

    def _focus_image_search(self, _event=None):
        if self._current_module not in self.IMAGE_SECTIONS:
            self.show_panel(self._workspace_section)
        self._image_search_entry.focus_set()
        self._image_search_entry.selection_range(0, "end")
        return "break"

    def _reload_recursive(self):
        if self.dir_var.get():
            self.load_image_dir(self.dir_var.get())

    def _on_edit_mode_changed(self):
        if self.edit_mode_var.get():
            path = self.image_paths[self.current_index] if 0 <= self.current_index < len(self.image_paths) else ""
            if not path:
                self.edit_mode_var.set(False)
                self.notify_flow("[编辑] 请先打开图片文件夹")
            elif self.editor_tool_var.get() == "浏览":
                self.editor_tool_var.set("选择 / 编辑")
        if not self.edit_mode_var.get():
            self.editor_tool_var.set("浏览")
        self._editor_cancel_drawing()
        self.canvas.delete("polygon_preview")
        self._edit_sel = None
        self._focus_shape = None
        editing = self.edit_mode_var.get()
        if editing:
            self.labels_visible.set(True)
            self.show_gt_var.set(True)
        self._editor_update_hint()
        if editing:
            self.show_panel("labels")
        self.render_image()

    def _toggle_log_command(self):
        self._log_expanded.set(not self._log_expanded.get())
        self._toggle_log()

    def _toggle_log(self):
        if self._log_expanded.get():
            self._log_frame.pack(fill="x")
        else:
            self._log_frame.pack_forget()
        self._log_toggle_btn.configure(text="收起日志" if self._log_expanded.get() else "展开日志")

    def _navigation_target(self, delta):
        if self._current_module in self.IMAGE_SECTIONS and self._image_search_var.get().strip():
            positions = self._library_positions
            if self.current_index not in positions:
                return self._library_indices[0 if delta > 0 else -1] if self._library_indices else None
            pos = positions[self.current_index] + delta
            return self._library_indices[pos] if 0 <= pos < len(self._library_indices) else None
        idx = self.current_index + delta
        return idx if 0 <= idx < len(self.image_paths) else None

    def _update_overview(self, path="", width=0, height=0, size=0):
        total = len(self.image_paths)
        self._img_count_label.configure(text=f"图片: {total}")
        self._cur_idx_label.configure(text=f"当前: {self.current_index + 1 if total else 0}/{total}")
        self._loaded_count_var.set(str(total))
        text = f"文件：{Path(path).name}\n尺寸：{width} × {height}\n大小：{size / 1024:.1f} KB\n路径：{path}" if path else "尚未选择图片"
        self._img_info_text.configure(state="normal")
        self._img_info_text.delete("1.0", "end")
        self._img_info_text.insert("1.0", text)
        self._img_info_text.configure(state="disabled")

    def _resolve_pending_edits(self):
        """切图、换目录、关闭前处理修改（自动保存常开，直接保存）；保存失败时留在当前图片。"""
        if not self._edit_dirty:
            return True
        self._save_current_labels(force=True)
        return not self._edit_dirty
