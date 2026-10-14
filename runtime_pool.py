"""Bounded shared daemon workers for immediate and scheduled Subagent runs."""
from concurrent.futures import Executor, Future
import queue,threading,time

class ParallelDaemonExecutor(Executor):
    def __init__(self,max_workers=4):
        if not isinstance(max_workers,int) or not 1<=max_workers<=16:raise ValueError('并行任务数必须为 1 至 16。')
        self.max_workers=max_workers;self._queue=queue.Queue();self._lock=threading.Lock();self._closed=False;self._active=0
        self._threads=[threading.Thread(target=self._work,name='agent-task-'+str(i+1),daemon=True) for i in range(max_workers)]
        for thread in self._threads:thread.start()
    def submit(self,fn,*args,**kwargs):
        future=Future()
        with self._lock:
            if self._closed:raise RuntimeError('本地服务正在关闭。')
            self._queue.put((future,fn,args,kwargs))
        return future
    def _work(self):
        while True:
            item=self._queue.get()
            try:
                if item is None:return
                future,fn,args,kwargs=item
                if not future.set_running_or_notify_cancel():continue
                with self._lock:self._active+=1
                try:future.set_result(fn(*args,**kwargs))
                except BaseException as error:future.set_exception(error)
                finally:
                    with self._lock:self._active-=1
            finally:self._queue.task_done()
    def status(self):
        with self._lock:return {'limit':self.max_workers,'active':self._active,'queued':self._queue.qsize() if not self._closed else 0}
    def join(self,timeout=None):
        deadline=None if timeout is None else time.monotonic()+timeout
        for thread in self._threads:
            if thread is not threading.current_thread():thread.join(None if deadline is None else max(0,deadline-time.monotonic()))
    def shutdown(self,wait=True,*,cancel_futures=False):
        with self._lock:
            if not self._closed:
                self._closed=True
                if cancel_futures:
                    while True:
                        try:item=self._queue.get_nowait()
                        except queue.Empty:break
                        if item is not None:item[0].cancel()
                        self._queue.task_done()
                for _ in self._threads:self._queue.put(None)
        if wait:
            for thread in self._threads:
                if thread is not threading.current_thread():thread.join()
