import base64
import json
import shutil
import time
from pathlib import Path
import pytest
import pymupdf as fitz
from app import create_app
from app.detector import classify, assemble

FRONT = 'Prefeitura requerimento de isencao de IPTU nome cadastro do imovel'
CONT = 'Prefeitura conjuge dependente trabalha socio de empresa imovel'
END = 'Assinatura do requerente presente requerimento lei complementar isencao IPTU'

class Source:
    items = {}
    def __init__(self, config):
        pass
    def test(self):
        return ['processo']
    def inventory(self):
        return self.items
    def copy(self, item, destination):
        shutil.copyfile(item['path'], destination)

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_SECRET', 'x' * 48)
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-123')
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    def reader(page, *args):
        text = page.get_text()
        return text, classify(text), 0
    app = create_app(tmp_path / 'data', Source, reader)
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

def configure(client):
    return client.post('/api/config', json=dict(server='server', share='docs', subpath='IPTU',
        username='reader', password='network-secret-123', stable_seconds=120))

def test_auth_csrf_and_encrypted_credentials(client):
    assert client.get('/api/state', headers={'Authorization': ''}).status_code == 401
    assert client.get('/healthz', headers={'Authorization': ''}).status_code == 200
    assert client.post('/api/config', json={}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert configure(client).status_code == 200
    body = client.get('/api/config').json
    assert body['has_password'] and 'password' not in body
    root = client.application.extensions['iptu']['root']
    for path in root.glob('state.sqlite3*'):
        assert b'network-secret-123' not in path.read_bytes()
    assert client.post('/api/config', json=dict(server='server', share='docs', username='reader',
        stable_seconds=120, password='')).status_code == 200
    assert client.get('/api/config').json['has_password']

def test_full_workflow_split_reversed_forms_resume(client, tmp_path):
    configure(client)
    files = []
    # Same process, different files; continuation precedes front in source order.
    for index, text in enumerate([CONT + ' ' + END, FRONT]):
        path = tmp_path / f'{index}.pdf'
        with fitz.open() as doc:
            p = doc.new_page()
            p.insert_text((30, 40), text, fontsize=8)
            doc.save(path)
        files.append(dict(path=str(path), relative=path.name, size=path.stat().st_size, mtime=0))
    Source.items = {'processo-01': files}
    assert client.post('/api/scan', json={}).status_code == 202
    assert wait(client)['counts'] == {'pendente': 1}
    assert client.post('/api/run', json={'limit':1}).status_code == 202
    state = wait(client)
    assert state['counts'] == {'completo_provavel':1}, state
    row = state['rows'][0]
    data = client.get(f"/results/{row['id']}/pdf").data
    with fitz.open(stream=data, filetype='pdf') as doc:
        assert len(doc) == 2 and 'nome' in doc[0].get_text()
    manifest = client.get(f"/results/{row['id']}/json").json
    assert manifest['selected'][0]['file_index'] == 1
    client.post('/api/scan', json={})
    assert wait(client)['counts'] == {'completo_provavel':1}
    assert 'processo-01' in client.get('/api/export.csv').data.decode('utf-8-sig')
    assert client.get('/results/invalid/pdf').status_code == 404
    assert client.post(f"/api/retry/{row['id']}", json={}).status_code == 200
    assert client.get(f"/results/{row['id']}/pdf").status_code == 404

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
    assert client.post('/api/config',json={'server':'smb://bad'}).status_code == 400

def test_failed_pdf_does_not_hide_missing_documents(client, tmp_path):
    configure(client)
    path=tmp_path/'broken.pdf'
    path.write_text('not a pdf')
    Source.items={'bad-process':[dict(path=str(path),relative='broken.pdf',size=9,mtime=0)]}
    client.post('/api/scan',json={});wait(client)
    client.post('/api/run',json={'limit':1})
    assert wait(client)['counts']=={'erro':1}

def test_inventory_changes_requeue_and_new_source_hides_old(client, tmp_path):
    configure(client)
    item=dict(path='/not-read-on-scan.pdf',relative='a.pdf',size=10,mtime=10)
    Source.items={'p':[item]}
    client.post('/api/scan',json={});wait(client)
    db=client.application.extensions['iptu']['db']
    with db() as con:
        con.execute("UPDATE processes SET status='nao_encontrado'")
    Source.items={'p':[{**item,'size':20}]}
    client.post('/api/scan',json={})
    assert wait(client)['counts']=={'pendente':1}
    assert client.post('/api/config',json=dict(server='other-server',share='docs',username='reader',stable_seconds=120)).status_code==200
    assert client.get('/api/state').json['counts']=={}

def test_smb_path_validation():
    from app.source import validate
    valid=dict(server='192.168.0.1',share='Documentos Públicos',subpath='Isencao_IPTU',username='reader',stable_seconds=120)
    assert validate(valid.copy())['share']=='Documentos Públicos'
    for changes in [dict(server='smb://x'),dict(share='../bad'),dict(subpath='../escape'),dict(subpath='C:\\data')]:
        with pytest.raises(ValueError):
            validate({**valid,**changes})

def test_exclusive_worker_and_cooperative_stop(client, monkeypatch):
    import threading
    gate=threading.Event()
    monkeypatch.setattr(Source,'test',lambda self: (gate.wait(2), [])[1])
    client.post('/api/test',json={})
    assert client.post('/api/scan',json={}).status_code==409
    assert configure(client).status_code==409
    assert client.post('/api/stop',json={}).status_code==200
    gate.set()
    state=wait(client)
    assert not state['runtime']['running']

def test_continuation_legal_text_is_not_a_second_front():
    text = CONT + ' nome ' + END + ' O presente requerimento fundamenta-se na Lei Complementar para isencao de IPTU'
    decision = classify(text)
    assert 'inicio' not in decision['roles']
    assert set(decision['roles']) == {'continuacao', 'encerramento'}

def test_ocr_review_and_exports_preserve_raw_json(client, tmp_path, monkeypatch):
    from app.extraction import empty_document,summarize
    configure(client)
    path=tmp_path/'input.pdf'
    with fitz.open() as doc:
        for text in (FRONT,CONT+' '+END):
            p=doc.new_page();p.insert_text((30,40),text,fontsize=8)
        doc.save(path)
    Source.items={'p':[dict(path=str(path),relative='input.pdf',size=path.stat().st_size,mtime=0)]}
    client.post('/api/scan',json={});wait(client)
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
    csrf=client.post('/api/test',json={},headers={'X-CSRF-Token':''})
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
    assert client.post('/api/config',json={},headers={'Authorization':auth}).status_code==403
