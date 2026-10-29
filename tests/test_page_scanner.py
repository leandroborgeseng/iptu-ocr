import threading
import pymupdf as fitz
import pytest
from app.detector import read_page
from app.page_scanner import PageScanner


def make_document(path):
    texts=[
        'Prefeitura requerimento de isencao de IPTU nome cadastro do imovel',
        'Prefeitura conjuge dependente trabalha socio de empresa imovel',
        'Assinatura do requerente presente requerimento lei complementar isencao IPTU',
    ]
    with fitz.open() as document:
        for text in texts:
            page=document.new_page()
            page.insert_textbox(fitz.Rect(30,30,550,750),text+'\n'+'Texto ficticio para comparacao do processamento.\n'*5)
        document.save(path)


def test_real_process_workers_match_serial_and_preserve_page_order(tmp_path):
    path=tmp_path/'input.pdf';make_document(path)
    serial=PageScanner(1);parallel=PageScanner(2)
    try:
        expected,_=serial.scan(path,tmp_path,read_page,threading.Event(),lambda *_:None)
        observed,timing=parallel.scan(path,tmp_path,read_page,threading.Event(),lambda *_:None)
        assert observed==expected
        assert [p['page'] for p in observed]==[1,2,3]
        assert timing['workers']==2 and timing['pages']==3
        pool=parallel.pool
        repeated,_=parallel.scan(path,tmp_path,read_page,threading.Event(),lambda *_:None)
        assert repeated==expected and parallel.pool is pool
    finally:
        serial.close();parallel.close()


def test_cancelled_scan_does_not_return_partial_document(tmp_path):
    path=tmp_path/'input.pdf';make_document(path)
    scanner=PageScanner(2);cancel=threading.Event();cancel.set()
    try:
        with pytest.raises(InterruptedError):scanner.scan(path,tmp_path,read_page,cancel,lambda *_:None)
    finally:scanner.close()


def test_failed_pool_is_discarded_so_later_uploads_can_retry(tmp_path,monkeypatch):
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool
    from app.diagnostics import ProcessingDiagnosticError
    import app.page_scanner as module
    path=tmp_path/'input.pdf';make_document(path)
    class FailedPool:
        def __init__(self,**kwargs):pass
        def submit(self,*args):
            future=Future();future.set_exception(BrokenProcessPool('test worker failure'));return future
        def shutdown(self,**kwargs):pass
    monkeypatch.setattr(module,'ProcessPoolExecutor',FailedPool)
    scanner=PageScanner(2)
    try:
        with pytest.raises(ProcessingDiagnosticError) as error:scanner.scan(path,tmp_path,read_page,threading.Event(),lambda *_:None)
        assert error.value.code=='page_worker_failed' and scanner.pool is None
    finally:scanner.close()
