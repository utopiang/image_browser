"""Independent crash-recovery drafts; never replace original annotations."""
import hashlib
import json
import threading
from dataclasses import asdict
from pathlib import Path
from .annotation_formats import atomic_json, LabelShape
from .latest_worker import LatestWorker


class DraftStore:
    def __init__(self, root):
        self.root = Path(root)
        self.worker = LatestWorker("RecoveryDraft")
        self.lock = threading.Lock()
        self.last_error = None

    def path_for(self, image, source):
        key = hashlib.sha256((str(Path(image).resolve())+"\0"+source).encode("utf-8")).hexdigest()
        return self.root / (key+".json")

    def save(self, image, source, shapes):
        document = {"schema":1,"image":image,"source":source,"shapes":[asdict(s) for s in shapes]}
        target = self.path_for(image,source)
        def write(valid):
            with self.lock:
                if valid():
                    try:
                        atomic_json(target,document)
                        self.last_error = None
                    except (OSError,ValueError,TypeError) as exc:
                        self.last_error = str(exc)
        self.worker.submit(write)

    def load(self, image, source):
        path = self.path_for(image,source)
        if not path.is_file(): return None
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("image") != image or document.get("source") != source or document.get("schema")!=1:
            raise ValueError("恢复草稿与当前数据源不一致")
        return [LabelShape(**s) for s in document["shapes"]]

    def clear(self, image, source):
        self.worker.invalidate()
        with self.lock:
            self.path_for(image,source).unlink(missing_ok=True)

    def close(self):
        self.worker.close()
