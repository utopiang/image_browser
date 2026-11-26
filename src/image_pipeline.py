"""Current original plus two nearby previews; slider scrubbing never commits edits."""
from collections import OrderedDict
from pathlib import Path
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk
from .latest_worker import LatestWorker


def decode_preview(path, limit=(960, 600)):
    with Image.open(path) as source:
        original = source.size
        source.draft("RGB", limit)
        source.thumbnail(limit, Image.Resampling.BILINEAR)
        preview = source.convert("RGB")
    return preview, original


class ImagePipelineMixin:
    def _init_image_pipeline(self):
        self._full_loader = LatestWorker("CurrentImage")
        self._preview_loader = LatestWorker("NearbyPreview")
        self._preview_cache = OrderedDict()
        self._full_generation = 0
        self._slider_generation = 0
        self._slider_target = None
        self._slider_preview_after = None
        self._nearby_after = None
        self._slider_photo = None
        self._slider_dragging = False
        self._setting_slider = False
        self._slider_caption = tk.StringVar()
        ttk.Label(self.image_slider_frame, textvariable=self._slider_caption, style="Hint.TLabel").pack(side="right", padx=8)
        self.image_slider_scale.bind("<ButtonPress-1>", self._slider_press, add="+")
        self.image_slider_scale.bind("<ButtonRelease-1>", self._slider_release, add="+")

    def _invalidate_image_pipeline(self):
        self._full_generation += 1
        self._slider_generation += 1
        self._slider_target = None
        self._full_loader.invalidate()
        self._preview_loader.invalidate()
        for name in ("_slider_preview_after", "_nearby_after"):
            value = getattr(self, name, None)
            if value:
                self.root.after_cancel(value)
                setattr(self, name, None)
        self._preview_cache.clear()
        self.canvas.delete("slider_preview")
        self._slider_photo = None

    def _close_image_pipeline(self):
        self._invalidate_image_pipeline()
        self._full_loader.close()
        self._preview_loader.close()

    def _load_current_original(self, path):
        self._full_generation += 1
        generation = self._full_generation
        self._preview_loader.invalidate()
        self._current_loading_path = path
        self._image_loading = True
        def load(valid):
            image = None
            try:
                if not valid(): return
                with Image.open(path) as source:
                    if not valid(): return
                    image = source.convert("RGB")
                if not valid() or self._closing:
                    image.close(); return
                self.task_queue.put(("original_loaded", (generation, path, image, None)))
            except Exception as exc:
                if image is not None: image.close()
                if valid() and not self._closing:
                    self.task_queue.put(("original_loaded", (generation, path, None, str(exc))))
        self._full_loader.submit(load)

    def _apply_original_loaded(self, payload):
        generation, path, image, error = payload
        current = self.image_paths[self.current_index] if 0 <= self.current_index < len(self.image_paths) else None
        if generation != self._full_generation or path != current or self._closing:
            if image is not None: image.close()
            return
        self._image_loading = False
        if error:
            self._skip_corrupted_image(path, error)
            return
        self._original_sizes[path] = image.size
        self._image_cache.clear()
        self._cache_loaded_image(path, image)
        self.canvas.delete("slider_preview")
        self._slider_photo = None
        self._on_image_loaded(path)

    def _schedule_nearby_previews(self):
        if self._closing or self._image_loading or self._slider_target is not None: return
        if self._nearby_after:
            self.root.after_cancel(self._nearby_after)
        self._nearby_after = self.root.after(250, self._load_nearby_previews)

    def _load_nearby_previews(self):
        self._nearby_after = None
        if self._closing or self._image_loading or self._slider_target is not None: return
        center = self.current_index
        paths = tuple(self.image_paths[i] for i in (center-1, center+1) if 0 <= i < len(self.image_paths))
        for key in list(self._preview_cache):
            if key not in paths: del self._preview_cache[key]
        missing = tuple(p for p in paths if p not in self._preview_cache)
        if not missing: return
        generation = self._full_generation
        def load(valid):
            for path in missing:
                if not valid() or self._closing: return
                try:
                    image, size = decode_preview(path)
                    if not valid() or self._closing:
                        image.close(); return
                    self.task_queue.put(("nearby_preview", (generation, path, image, size)))
                except (OSError, ValueError):
                    # Optional previews must not interrupt normal browsing.
                    continue
        self._preview_loader.submit(load)

    def _apply_nearby_preview(self, payload):
        generation, path, image, size = payload
        neighbors = {self.image_paths[i] for i in (self.current_index-1,self.current_index+1)
                     if 0 <= i < len(self.image_paths)}
        if generation == self._full_generation and path in neighbors and self._slider_target is None:
            self._preview_cache[path] = image
            self._original_sizes[path] = size
            while len(self._preview_cache) > 2: self._preview_cache.popitem(last=False)
        else: image.close()

    def _slider_press(self, _event=None):
        self._slider_dragging = True

    def _slider_release(self, _event=None):
        self._slider_dragging = False
        self._commit_slider_target()

    def _on_image_scale_change(self, value):
        if getattr(self,"_setting_slider",False) or not self.image_paths: return
        index = min(len(self.image_paths)-1, max(0, round(float(value)/1000*(len(self.image_paths)-1))))
        if not self._slider_dragging and index == self.current_index: return
        self._on_image_slider_changed(float(value), 1000)

    def _on_image_slider_changed(self, value, max_val):
        if not self.image_paths or max_val <= 0: return
        index = min(len(self.image_paths)-1,max(0,round(value/max_val*(len(self.image_paths)-1))))
        self._slider_target = index
        self._slider_generation += 1
        self._preview_loader.invalidate()
        self._slider_caption.set(f"{index+1} / {len(self.image_paths)}")
        if self._slider_preview_after:
            self.root.after_cancel(self._slider_preview_after)
        path = self.image_paths[index]
        if path in self._preview_cache:
            self._display_slider_preview(self._preview_cache[path])
        self._slider_preview_after = self.root.after(180, self._preview_slider_target)
        if not self._slider_dragging:
            self._commit_slider_target()

    def _preview_slider_target(self):
        self._slider_preview_after = None
        index = self._slider_target
        if index is None or not 0 <= index < len(self.image_paths): return
        path = self.image_paths[index]
        if path in self._preview_cache:
            self._display_slider_preview(self._preview_cache[path]); return
        generation = self._slider_generation
        def load(valid):
            try:
                if not valid(): return
                image, size = decode_preview(path)
                if not valid() or self._closing:
                    image.close(); return
                self.task_queue.put(("slider_preview",(generation,index,image)))
            except (OSError,ValueError): pass
        self._preview_loader.submit(load)

    def _apply_slider_preview(self, payload):
        generation,index,image = payload
        if generation == self._slider_generation and index == self._slider_target:
            self._display_slider_preview(image)
        image.close()

    def _display_slider_preview(self, image, caption="预览 · 松开加载清晰图片"):
        width,height = max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())
        self.canvas.delete("slider_preview")
        self.canvas.create_rectangle(0,0,width,height,fill="#202124",outline="",tags="slider_preview")
        ratio = min(width/image.width,height/image.height,1.)
        display = image.resize((max(1,int(image.width*ratio)),max(1,int(image.height*ratio))),Image.Resampling.BILINEAR)
        self._slider_photo = ImageTk.PhotoImage(display)
        display.close()
        self.canvas.create_image(width/2,height/2,image=self._slider_photo,tags="slider_preview")
        self.canvas.create_text(width/2,24,text=caption,fill="#ffffff",tags="slider_preview")

    def _commit_slider_target(self):
        index = self._slider_target
        if index is None: return
        self._slider_target = None
        self._slider_generation += 1
        self._preview_loader.invalidate()
        if self._slider_preview_after:
            self.root.after_cancel(self._slider_preview_after)
            self._slider_preview_after = None
        self.canvas.delete("slider_preview")
        self._slider_photo = None
        self._slider_caption.set("")
        if not 0 <= index < len(self.image_paths): return
        self.current_index = index
        if self._current_module == "dedupe":
            self._focus_dedupe_image(self.image_paths[index])
        else:
            self.show_current_image()
