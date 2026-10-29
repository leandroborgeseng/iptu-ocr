"""Bounded process workers: every worker opens its own PDF (no shared PyMuPDF objects)."""
import atexit
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
import pymupdf as fitz
from .detector import read_page
from .diagnostics import ProcessingDiagnosticError


def scan_page(path, directory, number):
    # Each OCR command uses one CPU thread; concurrency is controlled here.
    os.environ['OMP_THREAD_LIMIT']='1'
    with fitz.open(path) as document:
        _,decision,rotation=read_page(document[number],Path(directory),str(number))
    return dict(page=number+1,rotation=rotation,**decision)


class PageScanner:
    def __init__(self,workers=2):
        self.workers=workers
        self.pool=None
        atexit.register(self.close)

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=True,cancel_futures=True)
            self.pool=None

    def scan(self,path,directory,reader,cancel,progress):
        started=time.monotonic()
        with fitz.open(path) as document:
            if document.needs_pass:raise ValueError('PDF protegido por senha.')
            total=len(document)
            # Small documents and injected readers avoid process startup and serialization.
            if self.workers==1 or total<=2 or reader is not read_page:
                pages=[]
                for number,page in enumerate(document):
                    if cancel.is_set():raise InterruptedError()
                    progress(number,total)
                    _,decision,rotation=reader(page,Path(directory),str(number))
                    pages.append(dict(page=number+1,rotation=rotation,**decision))
                return pages,dict(seconds=round(time.monotonic()-started,2),pages=total,workers=1)
        if self.pool is None:
            self.pool=ProcessPoolExecutor(max_workers=self.workers,mp_context=multiprocessing.get_context('spawn'))
        pending={};results=[];next_page=0
        try:
            while next_page<total or pending:
                if cancel.is_set():raise InterruptedError()
                # Never enqueue an entire large PDF; at most one active page per worker.
                while next_page<total and len(pending)<self.workers:
                    future=self.pool.submit(scan_page,str(path),str(directory),next_page)
                    pending[future]=next_page;next_page+=1
                completed,_=wait(pending,timeout=.25,return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.pop(future)
                    results.append(future.result())
                    progress(len(results),total)
            return sorted(results,key=lambda p:p['page']),dict(seconds=round(time.monotonic()-started,2),pages=total,workers=self.workers)
        except BrokenProcessPool as exc:
            broken=self.pool;self.pool=None
            broken.shutdown(wait=False,cancel_futures=True)
            raise ProcessingDiagnosticError('page_worker_failed','Um trabalhador de páginas foi encerrado. Confira memória/CPU do contêiner; reduza DETECTOR_WORKERS para 1 se necessário e retome o envio.') from exc
        finally:
            # Keep files alive until running workers release them, including on cancellation.
            for future in pending:future.cancel()
            if pending:wait(pending)
