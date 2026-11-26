"""A single worker with one replaceable pending job and cooperative cancellation."""
import threading


class LatestWorker:
    def __init__(self, name):
        self._condition = threading.Condition()
        self._ticket = 0
        self._pending = None
        self._closed = False
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def invalidate(self):
        with self._condition:
            self._ticket += 1
            self._pending = None
            return self._ticket

    def submit(self, job):
        with self._condition:
            if self._closed:
                return None
            self._ticket += 1
            ticket = self._ticket
            self._pending = ticket, job
            self._condition.notify()
            return ticket

    def current(self, ticket):
        with self._condition:
            return not self._closed and ticket == self._ticket

    def close(self):
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                ticket, job = self._pending
                self._pending = None
            try:
                job(lambda: self.current(ticket))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Background job failed")
