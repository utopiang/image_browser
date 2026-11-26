import tempfile
import threading
import time
import unittest
from pathlib import Path
from PIL import Image
from src.latest_worker import LatestWorker
from src.image_pipeline import decode_preview
from src.recovery import DraftStore
from src.annotation_formats import LabelShape, rectangle


class PipelineTests(unittest.TestCase):
    def test_latest_pending_replaces_intermediate_jobs(self):
        worker=LatestWorker("test-latest")
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        executed=[]
        def running(valid):
            entered.set();release.wait(2)
            if valid(): executed.append("obsolete")
        worker.submit(running)
        self.assertTrue(entered.wait(1))
        for i in range(100):
            worker.submit(lambda valid,i=i: executed.append(i) if valid() else None)
        worker.submit(lambda valid: (executed.append("last"),finished.set()) if valid() else None)
        release.set()
        self.assertTrue(finished.wait(2))
        worker.close();worker.thread.join(1)
        self.assertEqual(executed,["last"])
        self.assertFalse(worker.thread.is_alive())

    def test_preview_is_small_and_keeps_original_dimensions(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"large.jpg"
            image=Image.new("RGB",(8192,4000),"gray");image.save(path);image.close()
            preview,size=decode_preview(path)
            self.assertEqual(size,(8192,4000))
            self.assertLessEqual(preview.width,960)
            self.assertLessEqual(preview.height,600)
            self.assertLess(preview.width*preview.height*3,2*1024*1024)
            preview.close()

    def test_draft_roundtrip_and_successful_save_removes_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DraftStore(folder)
            shape=LabelShape(0,"water","detect",rectangle(.1,.2,.3,.4))
            store.save("image.jpg","labels",[shape])
            path=store.path_for("image.jpg","labels")
            deadline=time.monotonic()+2
            while not path.exists() and time.monotonic()<deadline: time.sleep(.01)
            restored=store.load("image.jpg","labels")
            self.assertEqual(restored[0].class_name,"water")
            self.assertEqual(len(restored[0].points),4)
            store.clear("image.jpg","labels")
            self.assertIsNone(store.load("image.jpg","labels"))
            store.close();store.worker.thread.join(1)

    def test_invalidated_decode_does_not_publish_old_target(self):
        worker=LatestWorker("test-cancel")
        entered,release=threading.Event(),threading.Event()
        results=[]
        def load(valid):
            entered.set();release.wait(1)
            if valid(): results.append("old")
        worker.submit(load);self.assertTrue(entered.wait(1))
        worker.invalidate();release.set()
        worker.close();worker.thread.join(1)
        self.assertFalse(results)

if __name__=="__main__": unittest.main()
