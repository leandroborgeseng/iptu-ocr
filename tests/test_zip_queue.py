import hashlib
import io
import json
import threading
import time
import zipfile
from pathlib import Path
import pytest
from app import create_app as factory
from app.detector import classify
from test_app import client,pdf_bytes,FRONT,CONT,END,authenticated_test_client


def zip_bytes(entries):
    target=io.BytesIO()
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for path,data in entries:archive.writestr(path,data)
    return target.getvalue()


def begin(client,data,name='lote.zip',**options):
    response=client.post('/api/archives',json=dict(name=name,size=len(data),sha256=hashlib.sha256(data).hexdigest(),**options))
    assert response.status_code==201,response.json
    return response.json


def chunk(client,identifier,data,offset=0):
    return client.post(f'/api/archives/{identifier}/chunks?offset={offset}',data=data,content_type='application/octet-stream')


def submit(client,data,name='lote.zip',**options):
    started=begin(client,data,name,**options)
    assert chunk(client,started['id'],data).status_code==200
    response=client.post(f"/api/archives/{started['id']}/complete",json={})
    assert response.status_code==200,response.json
    return started['id']


def reader(page,*args):
    text=page.get_text();return text,classify(text),0


def test_zip_processes_recursively_without_browser_and_discards_only_successful_zip(client,tmp_path):
    data=zip_bytes([('Raiz/P1/anexos/frente.PDF',pdf_bytes(FRONT)),('Raiz/P1/verso.pdf',pdf_bytes(CONT+' '+END)),('Raiz/P2/a.pdf',pdf_bytes(FRONT,CONT+' '+END)),('Raiz/P1/ignore.txt',b'not a pdf'),('__MACOSX/._fake.pdf',b'ignored')])
    identifier=submit(client,data,extract_data=False)
    queue=client.application.extensions['iptu']['archives']
    assert queue.run_next() is True
    assert queue.run_next() is False
    state=client.get('/api/state').json
    assert state['total']==2 and all(row['status']=='completo_provavel' for row in state['rows'])
    root=client.application.extensions['iptu']['root']
    assert not list((root/'archives').iterdir()) and not list((root/'uploads').glob('*/files/*.pdf'))
    job=client.get('/api/archives').json['archives'][0]
    assert job['id']==identifier and job['status']=='done' and job['done']==2
    assert begin(client,data)['known'] is True


def test_zip_partial_upload_survives_restart_and_resumes_by_byte(client,tmp_path):
    data=zip_bytes([('P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END))]);started=begin(client,data)
    split=len(data)//2
    assert chunk(client,started['id'],data[:split]).json['received']==split
    root=client.application.extensions['iptu']['root']
    restarted=authenticated_test_client(factory(root,page_reader=reader,start_archive_queue=False))
    resumed=begin(restarted,data)
    assert resumed['id']==started['id'] and resumed['received']==split
    assert chunk(restarted,resumed['id'],data[split:],split).status_code==200
    assert restarted.post(f"/api/archives/{resumed['id']}/complete",json={}).status_code==200
    assert restarted.application.extensions['iptu']['archives'].run_next()
    assert restarted.get('/api/archives').json['counts']['done']==1


def test_invalid_pdf_does_not_block_following_zip_and_failed_zip_is_retained(client):
    bad=zip_bytes([('P1/a.pdf',b'not a PDF')]);bad_id=submit(client,bad,extract_data=False)
    good=zip_bytes([('P2/a.pdf',pdf_bytes(FRONT,CONT+' '+END))]);good_id=submit(client,good,'outro.zip',extract_data=False)
    queue=client.application.extensions['iptu']['archives']
    assert queue.run_next() and queue.get(bad_id)['status']=='error'
    assert queue.run_next() and queue.get(good_id)['status']=='done'
    assert queue.body(queue.get(bad_id)).exists()
    assert client.post('/api/archives/control',json={'action':'retry','id':bad_id}).status_code==200
    assert queue.get(bad_id)['status']=='queued'


@pytest.mark.parametrize('path',['../fora.pdf','/absoluto.pdf','C:\\fora.pdf'])
def test_zip_rejects_unsafe_pdf_paths(client,path):
    data=zip_bytes([(path,pdf_bytes(FRONT))]);identifier=begin(client,data)['id']
    chunk(client,identifier,data)
    assert client.post(f'/api/archives/{identifier}/complete',json={}).status_code==200
    assert client.application.extensions['iptu']['archives'].run_next()
    assert client.get('/api/archives').json['archives'][0]['status']=='error'


def test_zip_pause_is_persistent_and_processing_restart_keeps_completed_groups(client):
    data=zip_bytes([('R/P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END)),('R/P2/a.pdf',pdf_bytes(FRONT+' EXTRA',CONT+' '+END))]);identifier=submit(client,data,extract_data=False)
    queue=client.application.extensions['iptu']['archives'];queue.control('pause')
    assert not queue.run_next()
    root=client.application.extensions['iptu']['root']
    app=factory(root,page_reader=reader,start_archive_queue=False);other=authenticated_test_client(app);restored=app.extensions['iptu']['archives']
    assert restored.state()['paused'] is True and not restored.run_next()
    restored.control('resume');assert restored.run_next()
    with app.extensions['iptu']['db']() as con:con.execute("UPDATE archives SET status='processing' WHERE id=?",(identifier,))
    # Simulate crash before removal of a completed job: restoring has no new group work.
    job=restored.get(identifier);body=restored.directory/(identifier+'.zip');body.write_bytes(data)
    app2=factory(root,page_reader=reader,start_archive_queue=False);queue2=app2.extensions['iptu']['archives']
    assert queue2.get(identifier)['status']=='queued'
    assert queue2.run_next() and queue2.get(identifier)['status']=='done'
    assert app2.test_client().get('/healthz').status_code==200
    with app2.extensions['iptu']['db']() as con:assert con.execute('SELECT COUNT(*) FROM processes').fetchone()[0]==2


def test_chunk_offset_hash_validation_and_reset_cleanup(client):
    data=zip_bytes([('P1/a.pdf',pdf_bytes(FRONT))]);identifier=begin(client,data)['id']
    assert chunk(client,identifier,data,1).status_code==400
    assert chunk(client,identifier,b'x'*len(data)).status_code==200
    assert client.post(f'/api/archives/{identifier}/complete',json={}).status_code==200
    assert client.application.extensions['iptu']['archives'].run_next()
    assert begin(client,data)['received']==0
    assert client.post('/api/admin/reset-tests',json={'confirmation':'APAGAR TESTES'}).status_code==200
    assert client.get('/api/archives').json['archives']==[]


def test_flat_sei_zip_groups_all_files_as_one_process(client):
    data=zip_bytes([('a.pdf',pdf_bytes(FRONT)),('b.pdf',pdf_bytes(CONT+' '+END))]);identifier=submit(client,data,'3516200.410.00010907-2026-51.zip',extract_data=False)
    queue=client.application.extensions['iptu']['archives'];assert queue.run_next()
    rows=client.get('/api/state').json['rows']
    assert len(rows)==1 and rows[0]['name']=='3516200.410.00010907-2026-51' and rows[0]['file_count']==2


def test_real_scheduler_runs_automatically_after_receipt(client,tmp_path):
    root=tmp_path/'automatic';app=factory(root,page_reader=reader,start_archive_queue=True)
    other=authenticated_test_client(app);queue=app.extensions['iptu']['archives']
    try:
        identifier=submit(other,zip_bytes([('P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END))]),extract_data=False)
        for _ in range(300):
            if queue.get(identifier)['status'] in ('done','error'):break
            time.sleep(.01)
        assert queue.get(identifier)['status']=='done'
    finally:queue.close()


def test_automatic_ocr_json_and_cross_zip_duplicates_preserve_human_review(client,monkeypatch):
    import pymupdf as fitz
    calls=[]
    def fake_ocr(page,directory,index,dpi):
        calls.append(index);path=directory/f'fake-{index}.pdf'
        with fitz.open() as doc:doc.new_page();doc.save(path)
        return dict(text='OCR FICTICIO',words=[],boxes=[],width=2480,height=3508,pdf=path)
    monkeypatch.setattr('app.extraction.ocr_page',fake_ocr)
    pdf=pdf_bytes(FRONT,CONT+' '+END)
    first=submit(client,zip_bytes([('Raiz/P1/a.pdf',pdf)]))
    queue=client.application.extensions['iptu']['archives'];assert queue.run_next()
    row=client.get('/api/state').json['rows'][0];pid=row['id']
    assert row['result']['data'] and len(calls)==2
    assert client.post(f'/api/data/{pid}/review',json={'changes':{'requerente.nome':'REVISAO PRESERVADA'}}).status_code==200
    second=submit(client,zip_bytes([('OutraRaiz/P1/a.pdf',pdf),('nota.txt',b'different archive, same PDF')]),'outro.zip')
    assert first!=second and queue.run_next()
    assert len(calls)==2 and client.get('/api/state').json['total']==1
    assert client.get(f'/api/data/{pid}').json['dados']['requerente']['nome']=='REVISAO PRESERVADA'


def test_restart_resumes_only_unfinished_groups_after_pause(client):
    data=zip_bytes([('R/P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END)),('R/P2/a.pdf',pdf_bytes(FRONT+' SECOND',CONT+' '+END))])
    identifier=submit(client,data,extract_data=False);queue=client.application.extensions['iptu']['archives']
    original=queue.process
    def stop_after_first(*args):
        result=original(*args);queue.cancel.set();return result
    queue.process=stop_after_first
    assert queue.run_next() and queue.get(identifier)['status']=='queued'
    root=client.application.extensions['iptu']['root']
    first_row=client.get('/api/state').json['rows'][0]
    path=root/'results'/first_row['id']/'manifesto.json';before=path.read_bytes()
    restarted=factory(root,page_reader=reader,start_archive_queue=False);restored=restarted.extensions['iptu']['archives']
    restored.control('resume');assert restored.run_next()
    assert restored.get(identifier)['status']=='done' and path.read_bytes()==before
    with restarted.extensions['iptu']['db']() as con:assert con.execute('SELECT COUNT(*) FROM processes').fetchone()[0]==2


def test_upload_is_allowed_while_queue_worker_is_running(client):
    runtime=client.application.extensions['iptu']['runtime'];runtime['running']=True;runtime['operation']='arquivo_zip'
    try:
        data=zip_bytes([('P1/a.pdf',pdf_bytes(FRONT))]);identifier=submit(client,data,extract_data=False)
        assert client.application.extensions['iptu']['archives'].get(identifier)['status']=='validating'
        assert client.post('/api/admin/reset-tests',json={'confirmation':'APAGAR TESTES'}).status_code==409
    finally:runtime['running']=False


def test_manual_stop_pauses_archive_queue_persistently(client):
    data=zip_bytes([('P1/a.pdf',pdf_bytes(FRONT))]);submit(client,data,extract_data=False)
    runtime=client.application.extensions['iptu']['runtime'];runtime.update(operation='arquivo_zip',running=True)
    try:
        assert client.post('/api/stop',json={}).status_code==200
        assert client.get('/api/archives').json['paused'] is True
    finally:runtime['running']=False
    assert not client.application.extensions['iptu']['archives'].run_next()


def test_queue_limits_reject_oversized_admission_and_keep_existing_data(client):
    queue=client.application.extensions['iptu']['archives'];queue.max_bytes=10
    data=zip_bytes([('P1/a.pdf',pdf_bytes(FRONT))])
    response=client.post('/api/archives',json=dict(name='lote.zip',size=len(data),sha256=hashlib.sha256(data).hexdigest()))
    assert response.status_code==400 and client.get('/api/archives').json['archives']==[]
    queue.max_bytes=len(data)*2;queue.max_stored=len(data)-1
    assert client.post('/api/archives',json=dict(name='lote.zip',size=len(data),sha256=hashlib.sha256(data).hexdigest())).status_code==400


def test_failed_pdf_name_is_visible_in_queue_diagnostics(client):
    identifier=submit(client,zip_bytes([('P1/invalido.pdf',b'broken PDF')]),extract_data=False)
    queue=client.application.extensions['iptu']['archives'];assert queue.run_next()
    assert client.get('/api/archives').json['archives'][0]['issues'][0]['name']=='P1/invalido.pdf'


def test_complete_acknowledges_receipt_before_background_validation(client,monkeypatch):
    queue=client.application.extensions['iptu']['archives'];calls=[];inspect=queue.inspect
    def record_inspection(path):calls.append(path);return inspect(path)
    monkeypatch.setattr(queue,'inspect',record_inspection)
    data=zip_bytes([('P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END))]);identifier=submit(client,data,extract_data=False)
    assert queue.get(identifier)['status']=='validating' and calls==[]
    assert queue.run_next() and len(calls)==1 and queue.get(identifier)['status']=='done'
