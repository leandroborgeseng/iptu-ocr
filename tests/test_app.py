import base64
import json
import shutil
import time
from pathlib import Path
import pytest
import pymupdf as fitz
from app import create_app
from app.detector import classify, assemble

FRONT = 'Prefeitura requerimento de isencao de IPTU 2027 nome cadastro do imovel'
CONT = 'Prefeitura conjuge dependente trabalha socio de empresa imovel'
END = 'Assinatura do requerente presente requerimento lei complementar isencao IPTU'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_SECRET', 'x' * 48)
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-123')
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    def reader(page, *args):
        text = page.get_text()
        return text, classify(text), 0
    app = create_app(tmp_path / 'data',page_reader=reader)
    app.testing = True
    client = app.test_client()
    auth = 'Basic ' + base64.b64encode(b'admin:test-password-123').decode()
    client.environ_base['HTTP_AUTHORIZATION'] = auth
    client.get('/')
    with client.session_transaction() as sess:
        token = sess['csrf']
    client.environ_base['HTTP_X_CSRF_TOKEN'] = token
    client.application = app
    return client

def wait(client):
    for _ in range(300):
        state = client.get('/api/state').json
        if not state['runtime']['running']:
            return state
        time.sleep(.01)
    raise AssertionError('Worker did not finish')




def test_absent_incomplete_ambiguous():
    def record(text, page=1):
        return dict(file_index=0,page=page,**classify(text))
    assert assemble([record('certidao de obito')])[0] == 'nao_encontrado'
    assert assemble([record(FRONT)])[0] == 'incompleto'
    assert assemble([record(FRONT),record(FRONT,2),record(CONT,3),record(END,4)])[0] == 'revisao'
    assert assemble([record(FRONT),record(CONT,2),record(END,3)])[0] == 'completo_provavel'
    assert assemble([record('requerimento isencao iptu')])[0] == 'revisao'

def test_bad_batch_and_config(client):
    assert client.post('/api/run',json={'limit':0}).status_code == 400
    assert client.post('/api/run',json={'limit':501}).status_code == 400
    assert client.post('/api/config',json={'server':'smb://bad'}).status_code == 404





def test_continuation_legal_text_is_not_a_second_front():
    text = CONT + ' nome ' + END + ' O presente requerimento fundamenta-se na Lei Complementar para isencao de IPTU'
    decision = classify(text)
    assert 'inicio' not in decision['roles']
    assert set(decision['roles']) == {'continuacao', 'encerramento'}

def test_ocr_review_and_exports_preserve_raw_json(client, tmp_path, monkeypatch):
    from app.extraction import empty_document,summarize
    path=tmp_path/'input.pdf'
    with fitz.open() as doc:
        for text in (FRONT,CONT+' '+END):
            p=doc.new_page();p.insert_text((30,40),text,fontsize=8)
        doc.save(path)
    items=[('p/input.pdf',path.read_bytes())]
    batch=begin_test_upload(client,items);upload_test_file(client,batch,*items[0]);client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'limit':1});state=wait(client)
    pid=state['rows'][0]['id']
    calls=[]
    def fake_extract(pdf,manifest,output,**kwargs):
        calls.append(pid)
        result=summarize(empty_document(manifest['process'],manifest))
        result['paginas']=[dict(pagina=1)]
        (output/'dados.json').write_text(json.dumps(result))
        (output/'ocr.txt').write_text('OCR test')
        shutil.copyfile(pdf,output/'formulario_ocr.pdf')
        return result
    monkeypatch.setattr('app.extract_form',fake_extract)
    assert client.post('/api/ocr',json={'limit':1,'dpi':200}).status_code==400
    assert client.post('/api/ocr',json={'limit':1,'dpi':300}).status_code==202
    state=wait(client)
    assert state['rows'][0]['result']['data']
    assert client.get(f'/review/{pid}').status_code==200
    assert client.get(f'/api/page/{pid}/1.png').mimetype=='image/png'
    assert client.get(f'/api/page/{pid}/99.png').status_code==404
    assert client.post(f'/api/data/{pid}/review',json={'changes':{'requerente.nome':'CONFERIDO'}}).status_code==200
    assert client.get(f'/api/data/{pid}').json['dados']['requerente']['nome']=='CONFERIDO'
    assert client.get(f'/results/{pid}/dados').json['dados']['requerente']['nome']=='CONFERIDO'
    assert client.get(f'/results/{pid}/bruto').json['dados']['requerente']['nome'] is None
    assert json.loads(client.get('/api/export.jsonl').data.decode().strip())['dados']['requerente']['nome']=='CONFERIDO'
    client.post('/api/ocr',json={'limit':1});wait(client)
    assert len(calls)==1 # batch does not overwrite reviewed documents
    client.post(f'/api/retry/{pid}',json={})
    client.post('/api/run',json={'limit':1});wait(client)
    directory=client.application.extensions['iptu']['root']/'results'/pid
    versions=list((directory/'history').glob('*/dados.revisado.json'))
    assert len(versions)==1
    assert json.loads(versions[0].read_text())['dados']['requerente']['nome']=='CONFERIDO'

def test_api_errors_identify_panel_auth_and_csrf(client):
    unauth=client.get('/api/state',headers={'Authorization':''})
    assert unauth.status_code==401 and unauth.json['code']=='panel_auth_required'
    assert 'WWW-Authenticate' in unauth.headers
    csrf=client.post('/api/uploads',json={},headers={'X-CSRF-Token':''})
    assert csrf.status_code==403 and csrf.json['code']=='panel_session_invalid'
    assert 'COOKIE_SECURE' in csrf.json['error']
    assert not client.application.extensions['iptu']['runtime']['running']

def test_secure_cookie_configuration_is_visible_without_weakening_it(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_SECRET','x'*48)
    monkeypatch.setenv('ADMIN_PASSWORD','test-password-123')
    monkeypatch.setenv('COOKIE_SECURE','true')
    app=create_app(tmp_path/'data')
    client=app.test_client()
    auth='Basic '+base64.b64encode(b'admin:test-password-123').decode()
    response=client.get('/',headers={'Authorization':auth})
    assert b'name="secure-cookie" content="true"' in response.data
    assert 'Secure' in response.headers['Set-Cookie']
    assert client.post('/api/uploads',json={},headers={'Authorization':auth}).status_code==403





def pdf_bytes(*texts):
    with fitz.open() as document:
        for text in texts:
            page=document.new_page();page.insert_text((30,40),text,fontsize=8)
        return document.tobytes()


def begin_test_upload(client,items,grouping='auto'):
    response=client.post('/api/uploads',json=dict(name='Pasta teste',files=len(items),bytes=sum(len(data) for _,data in items),grouping=grouping))
    assert response.status_code==201,response.json
    return response.json['id']


def upload_test_file(client,batch,path,data):
    import io
    return client.post(f'/api/uploads/{batch}/files',data={'path':path,'file':(io.BytesIO(data),path.rsplit('/',1)[-1])})


def test_folder_upload_walks_deep_paths_and_processes_without_windows(client,monkeypatch):
    items=[('Isencao/PROCESSO-01/frente.pdf',pdf_bytes(FRONT)),
           ('Isencao/PROCESSO-01/anexos/profundos/continuacao.pdf',pdf_bytes(CONT+' '+END)),
           ('Isencao/PROCESSO-02/sub/pedido.pdf',pdf_bytes(FRONT,CONT+' '+END))]
    batch=begin_test_upload(client,items)
    for path,data in items:assert upload_test_file(client,batch,path,data).status_code==200
    result=client.post(f'/api/uploads/{batch}/finish',json={})
    assert result.status_code==200 and result.json['processes']==2
    assert client.post('/api/run',json={'limit':10,'source':'upload','upload_id':batch}).status_code==202
    state=wait(client)
    assert state['counts']=={'completo_provavel':2},state
    assert client.get('/api/state?source=smb').status_code==400
    assert client.get('/api/state?source=upload').json['total']==2
    assert client.get('/api/config').status_code==404
    for row in state['rows']:
        assert row['source_kind']=='upload'
        manifest=client.get(f"/results/{row['id']}/json").json
        assert manifest['source_kind']=='upload' and manifest['source_id']==batch
    originals={p['relative'] for row in state['rows'] for p in client.get(f"/results/{row['id']}/json").json['sources']}
    assert 'Isencao/PROCESSO-01/anexos/profundos/continuacao.pdf' in originals


def test_upload_incomplete_and_idempotent_retry(client):
    first=pdf_bytes(FRONT);second=pdf_bytes(CONT+' '+END)
    items=[('Pasta/Processo/a.pdf',first),('Pasta/Processo/sub/b.pdf',second)]
    batch=begin_test_upload(client,items)
    assert upload_test_file(client,batch,*items[0]).status_code==200
    assert upload_test_file(client,batch,*items[0]).json['repeated']
    assert client.post(f'/api/uploads/{batch}/finish',json={}).status_code==409
    assert client.get('/api/state').json['total']==0
    assert upload_test_file(client,batch,*items[1]).status_code==200
    assert client.post(f'/api/uploads/{batch}/finish',json={}).json['processes']==1
    assert client.post(f'/api/uploads/{batch}/finish',json={}).json['processes']==1
    assert upload_test_file(client,batch,*items[1]).status_code==409


def test_upload_rejects_traversal_nonpdf_and_bad_content(client):
    data=pdf_bytes(FRONT);batch=begin_test_upload(client,[('p/a.pdf',data)])
    for path in ('../escape.pdf','/escape.pdf','C:/escape.pdf','p/../escape.pdf','p\\escape.pdf','p/a.txt'):
        assert upload_test_file(client,batch,path,data).status_code==400
    assert upload_test_file(client,batch,'p/a.pdf',b'not a pdf').status_code==400
    root=client.application.extensions['iptu']['root']
    assert not list((root/'uploads'/batch/'files').glob('*.part'))
    assert client.get('/api/uploads').json['uploads'][0]['received_files']==0




def test_upload_accepts_pdf_larger_than_json_request_limit(client):
    data=pdf_bytes(FRONT)+b'\n%padding'+b' '*300000
    batch=begin_test_upload(client,[('p/a.pdf',data)])
    assert upload_test_file(client,batch,'p/a.pdf',data).status_code==200
    assert client.post(f'/api/uploads/{batch}/finish',json={}).status_code==200


def test_upload_size_and_source_validation(client):
    assert client.post('/api/uploads',json={'name':'p','files':0,'bytes':0}).status_code==400
    assert client.post('/api/uploads',json={'name':'p','files':1,'bytes':10**20}).status_code==400
    assert client.get('/api/state?source=bad').status_code==400
    assert client.post('/api/run',json={'source':'bad'}).status_code==400
    assert client.post('/api/ocr',json={'source':'bad'}).status_code==400




def plan_test_files(client,batch,items):
    import hashlib
    entries=[dict(relative=path,size=len(data),sha256=hashlib.sha256(data).hexdigest()) for path,data in items]
    response=client.post(f'/api/uploads/{batch}/plan',json={'files':entries})
    assert response.status_code==200,response.json
    return response.json['files']


def authenticated_test_client(app):
    client=app.test_client();client.environ_base['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'admin:test-password-123').decode();client.get('/')
    with client.session_transaction() as session:client.environ_base['HTTP_X_CSRF_TOKEN']=session['csrf']
    return client


def test_known_pdf_is_not_uploaded_or_processed_again_after_restart(client):
    items=[('Raiz/PROCESSO-01/a.pdf',pdf_bytes(FRONT,CONT+' '+END))]
    batch=begin_test_upload(client,items)
    assert plan_test_files(client,batch,items)[0]['state']=='required'
    upload_test_file(client,batch,*items[0]);client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'source':'upload','limit':10});state=wait(client)
    row=state['rows'][0];pid=row['id'];root=client.application.extensions['iptu']['root']
    assert row['result']['originals_removed']
    assert not list((root/'uploads'/batch/'files').glob('*.pdf'))
    assert (root/'results'/pid/'formulario.pdf').exists()
    resumed=authenticated_test_client(create_app(root))
    renamed=[('Outra raiz/PROCESSO-01/renomeado.pdf',items[0][1])]
    second=begin_test_upload(resumed,renamed)
    assert plan_test_files(resumed,second,renamed)[0]['state']=='known'
    result=resumed.post(f'/api/uploads/{second}/finish',json={}).json
    assert result['unchanged']==1 and result['new']==0
    resumed.post('/api/run',json={'source':'upload','upload_id':second,'limit':10})
    final=wait(resumed)
    assert final['runtime']['total']==0 and final['rows'][0]['id']==pid
    assert not list((root/'uploads'/second/'files').glob('*.pdf'))


def test_partial_upload_reuses_existing_original_without_sending_bytes_again(client):
    items=[('Raiz/P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END))]
    first=begin_test_upload(client,items);plan_test_files(client,first,items);upload_test_file(client,first,*items[0])
    second=begin_test_upload(client,items)
    assert plan_test_files(client,second,items)[0]['state']=='received'
    assert client.post(f'/api/uploads/{second}/finish',json={}).status_code==200
    client.post('/api/run',json={'source':'upload','upload_id':second,'limit':10})
    assert wait(client)['counts']=={'completo_provavel':1}
    root=client.application.extensions['iptu']['root']
    assert not list((root/'uploads').glob('*/files/*.pdf'))


def test_resume_manifest_is_persistent_and_changed_file_is_not_accepted(client):
    import hashlib
    data=pdf_bytes(FRONT)
    payload=dict(name='Pasta',files=1,bytes=len(data),selection_key='a'*64)
    first=client.post('/api/uploads',json=payload).json['id']
    second=client.post('/api/uploads',json=payload)
    assert second.status_code==200 and second.json['id']==first and second.json['resumed']
    assert client.post(f'/api/uploads/{first}/plan',json={'files':[dict(relative='P/a.pdf',size=len(data),sha256='0'*64)]}).status_code==200
    assert upload_test_file(client,first,'P/a.pdf',data).status_code==409
    assert client.get('/api/uploads').json['uploads'][0]['received_files']==0


def test_adding_documents_reuses_retained_pages_after_originals_were_removed(client,monkeypatch):
    calls=[]
    def ocr_stub(page,directory,index,dpi):
        calls.append(index)
        output=directory/f'mock-{index}.pdf'
        with fitz.open() as doc:
            p=doc.new_page();p.insert_text((30,40),'OCR DEMO');doc.save(output)
        return dict(text='OCR DEMO',words=[],boxes=[],width=2480,height=3508,pdf=output)
    monkeypatch.setattr('app.extraction.ocr_page',ocr_stub)
    first_items=[('Raiz/P1/frente.pdf',pdf_bytes(FRONT))]
    batch=begin_test_upload(client,first_items);upload_test_file(client,batch,*first_items[0]);client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'source':'upload','extract_data':True,'limit':10});state=wait(client)
    assert state['counts']=={'incompleto':1} and len(calls)==1
    second_items=[('Raiz/P1/anexos/continuacao.pdf',pdf_bytes(CONT+' '+END))]
    second=begin_test_upload(client,second_items);upload_test_file(client,second,*second_items[0])
    assert client.post(f'/api/uploads/{second}/finish',json={}).json['updated']==1
    client.post('/api/run',json={'source':'upload','upload_id':second,'extract_data':True,'limit':10});state=wait(client)
    assert state['counts']=={'completo_provavel':1},state
    assert state['rows'][0]['result']['data'] and len(calls)==2 # only the newly added page was OCRed
    root=client.application.extensions['iptu']['root']
    assert not list((root/'uploads').glob('*/files/*.pdf'))
    assert client.get(f"/results/{state['rows'][0]['id']}/pdf").status_code==200


def test_identical_document_in_two_processes_reuses_ocr_and_rebases_json(client,monkeypatch):
    calls=[]
    def ocr_stub(page,directory,index,dpi):
        calls.append(index);path=directory/f'mock-{index}.pdf'
        with fitz.open() as d:d.new_page();d.save(path)
        return dict(text='OCR',words=[],boxes=[],width=2480,height=3508,pdf=path)
    monkeypatch.setattr('app.extraction.ocr_page',ocr_stub)
    data=pdf_bytes(FRONT,CONT+' '+END)
    items=[('Raiz/P1/a.pdf',data),('Raiz/P2/a.pdf',data)]
    batch=begin_test_upload(client,items);plan_test_files(client,batch,items)
    upload_test_file(client,batch,*items[0])
    assert plan_test_files(client,batch,[items[1]])[0]['state']=='received'
    client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'source':'upload','extract_data':True,'limit':10});state=wait(client)
    assert state['counts']=={'completo_provavel':2} and len(calls)==2
    for row in state['rows']:
        document=client.get(f"/results/{row['id']}/dados").json
        assert document['processo']==row['name']
        assert document['paginas'][0]['origem']['source'].startswith('Raiz/'+row['name']+'/')


def test_network_routes_removed_and_saved_credentials_erased(client):
    db=client.application.extensions['iptu']['db'];root=client.application.extensions['iptu']['root']
    with db() as con:con.execute('INSERT INTO settings VALUES(1,?)',(b'old-encrypted-credentials',))
    app=create_app(root)
    with app.extensions['iptu']['db']() as con:assert con.execute('SELECT COUNT(*) FROM settings').fetchone()[0]==0
    for route in ('/api/config','/api/test','/api/scan'):
        assert client.post(route,json={}).status_code==404
    assert b'connection-form' not in client.get('/').data


def test_round_preparation_cleans_originals_before_whole_selection_finishes(client):
    items=[('Raiz/P1/a.pdf',pdf_bytes(FRONT,CONT+' '+END)),('Raiz/P2/b.pdf',pdf_bytes(FRONT+' DIFFERENT',CONT+' '+END))]
    batch=begin_test_upload(client,items);plan_test_files(client,batch,items)
    upload_test_file(client,batch,*items[0])
    assert client.post(f'/api/uploads/{batch}/prepare',json={'limit':1}).status_code==202
    state=wait(client)
    assert state['runtime']['details']['prepared']==1 and state['runtime']['details']['removed_bytes']>0
    root=client.application.extensions['iptu']['root']
    assert not list((root/'uploads'/batch/'files').glob('*.pdf'))
    assert client.post(f'/api/uploads/{batch}/finish',json={}).status_code==409
    checkpoint=client.post(f'/api/uploads/{batch}/checkpoint',json={})
    assert checkpoint.status_code==200 and checkpoint.json['processes']==1
    client.post('/api/run',json={'upload_id':batch,'limit':1});state=wait(client)
    assert state['counts']=={'completo_provavel':1}
    upload_test_file(client,batch,*items[1]);client.post(f'/api/uploads/{batch}/prepare',json={'limit':1});wait(client)
    assert client.post(f'/api/uploads/{batch}/checkpoint',json={}).json['processes']==1
    assert client.post(f'/api/uploads/{batch}/finish',json={}).status_code==200


def test_selection_can_hold_36_gb_without_raising_per_file_json_limits(client):
    response=client.post('/api/uploads',json={'name':'Acervo 36 GB','files':6000,'bytes':36*1024**3})
    assert response.status_code==201
    limits=client.get('/api/uploads').json['limits']
    assert limits['batch_bytes']>=36*1024**3 and limits['file_bytes']==100*1024**2


def test_only_2027_form_is_exported_front_first_from_mixed_inverted_pdf(client):
    data=pdf_bytes(FRONT.replace('2027','2026'),CONT+' '+END+' exercicio 2026',CONT+' '+END+' exercicio 2027',FRONT)
    items=[('Raiz/P1/a.pdf',data)];batch=begin_test_upload(client,items)
    upload_test_file(client,batch,*items[0]);client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'limit':1});row=wait(client)['rows'][0]
    assert row['status']=='completo_provavel' and row['result']['pages']==2
    manifest=client.get('/results/'+row['id']+'/json').json
    assert [p['page'] for p in manifest['selected']]==[4,3] and manifest['target_year']==2027
    with fitz.open(stream=client.get('/results/'+row['id']+'/pdf').data,filetype='pdf') as document:
        assert 'requerimento' in document[0].get_text().lower()
        assert len(document)==2 and '2026' not in ''.join(p.get_text() for p in document)


def test_previous_policy_and_candidate_cache_are_rechecked_without_original_upload(client):
    items=[('Raiz/P1/a.pdf',pdf_bytes(CONT+' '+END,FRONT))]
    batch=begin_test_upload(client,items);upload_test_file(client,batch,*items[0]);client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'limit':1});row=wait(client)['rows'][0]
    root=client.application.extensions['iptu']['root'];db=client.application.extensions['iptu']['db']
    assert not list((root/'uploads'/batch/'files').glob('*.pdf'))
    with db() as con:
        old=dict(row['result']);old.pop('selection_policy')
        con.execute('UPDATE processes SET result=? WHERE id=?',(json.dumps(old),row['id']))
        for cached in con.execute('SELECT sha256,page_data FROM pdf_cache').fetchall():
            pages=json.loads(cached['page_data'])
            for p in pages:
                p['evidence'].pop('exercicio_regra',None);p['evidence'].pop('exercicios',None)
            con.execute('UPDATE pdf_cache SET page_data=? WHERE sha256=?',(json.dumps(pages),cached['sha256']))
    def reader(page,*_):
        text=page.get_text();return text,classify(text),0
    restarted=authenticated_test_client(create_app(root,page_reader=reader))
    assert restarted.get('/api/state').json['rows'][0]['status']=='pendente'
    restarted.post('/api/run',json={'limit':1});updated=wait(restarted)['rows'][0]
    assert updated['status']=='completo_provavel' and updated['result']['target_year']==2027
    manifest=restarted.get('/results/'+row['id']+'/json').json
    assert [p['page'] for p in manifest['selected']]==[2,1]


def test_reset_tests_clears_outputs_receipts_and_cache_but_preserves_external_original(client,tmp_path):
    data=pdf_bytes(FRONT,CONT+' '+END)
    external=tmp_path/'original.pdf';external.write_bytes(data)
    items=[('Raiz/P1/a.pdf',data)];batch=begin_test_upload(client,items)
    plan_test_files(client,batch,items);upload_test_file(client,batch,*items[0]);client.post(f'/api/uploads/{batch}/finish',json={})
    client.post('/api/run',json={'limit':1});wait(client)
    root=client.application.extensions['iptu']['root'];(root/'results'/'manual-review.json').write_text('{}')
    response=client.post('/api/admin/reset-tests',json={'confirmation':'APAGAR TESTES'})
    assert response.status_code==200 and response.json['deleted']['processes']==1
    assert client.get('/api/state').json['total']==0 and client.get('/api/uploads').json['uploads']==[]
    assert all(not list((root/name).iterdir()) for name in ('uploads','results','cache'))
    assert external.read_bytes()==data
    new_batch=begin_test_upload(client,items)
    assert plan_test_files(client,new_batch,items)[0]['state']=='required'
    assert upload_test_file(client,new_batch,*items[0]).status_code==200


def test_reset_requires_confirmation_and_idle_worker(client):
    runtime=client.application.extensions['iptu']['runtime']
    assert client.post('/api/admin/reset-tests',json={}).status_code==400
    assert client.post('/api/admin/reset-tests',json={'confirmation':'APAGAR TESTES'},headers={'X-CSRF-Token':''}).status_code==403
    runtime['running']=True
    try:assert client.post('/api/admin/reset-tests',json={'confirmation':'APAGAR TESTES'}).status_code==409
    finally:runtime['running']=False


def test_reset_rejects_overlapping_upload_request(client,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.document_cache import DocumentCache
    items=[('Raiz/P1/a.pdf',pdf_bytes(FRONT))];batch=begin_test_upload(client,items)
    entered=threading.Event();release=threading.Event();original=DocumentCache.get
    def paused_get(self,sha):
        entered.set()
        assert release.wait(5)
        return original(self,sha)
    monkeypatch.setattr(DocumentCache,'get',paused_get)
    other=authenticated_test_client(client.application)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending=executor.submit(plan_test_files,other,batch,items)
        try:
            assert entered.wait(5)
            assert client.post('/api/admin/reset-tests',json={'confirmation':'APAGAR TESTES'}).status_code==409
            assert len(client.get('/api/uploads').json['uploads'])==1
        finally:release.set()
        assert pending.result()[0]['state']=='required'
