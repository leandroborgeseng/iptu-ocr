import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import shutil
import re
import unicodedata
import tempfile
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, session
import pymupdf as fitz
from .diagnostics import explain_error, ProcessingDiagnosticError
from .uploads import relative_pdf_path, group_items, LocalUploadSource
from .document_cache import DocumentCache, DETECTOR_VERSION, PIPELINE_VERSION
from .upload_index import UploadIndex
from .detector import read_page, assemble
from .extraction import extract_form, apply_review


def create_app(data_dir=None, page_reader=read_page):
    secret = os.environ.get('APP_SECRET', '')
    password = os.environ.get('ADMIN_PASSWORD', '')
    if len(secret) < 32 or len(password) < 12:
        raise RuntimeError('Defina APP_SECRET (mínimo 32 caracteres) e ADMIN_PASSWORD (mínimo 12).')
    app = Flask(__name__)
    app.json.sort_keys = False
    app.secret_key = secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() == 'true',
        MAX_CONTENT_LENGTH=262144)
    root = Path(data_dir or os.environ.get('DATA_DIR', '/data'))
    root.mkdir(parents=True, exist_ok=True)
    (root / 'results').mkdir(exist_ok=True)
    uploads_root = root / 'uploads'
    uploads_root.mkdir(exist_ok=True)
    for stale in uploads_root.glob('*/files/*.part'):
        if not stale.is_symlink():stale.unlink(missing_ok=True)
    upload_lock = threading.Lock()
    upload_limits = dict(file_bytes=int(os.environ.get('UPLOAD_MAX_FILE_MB','100'))*1024*1024,
                         batch_bytes=int(os.environ.get('UPLOAD_MAX_BATCH_MB','65536'))*1024*1024,
                         files=int(os.environ.get('UPLOAD_MAX_FILES','20000')))
    mutex = threading.Lock()
    cancel = threading.Event()
    runtime = {'running': False, 'operation': '', 'current': '', 'done': 0, 'total': 0,
               'message': 'Pronto para receber pastas e PDFs.', 'error': False, 'error_code': None, 'diagnostics': []}

    @contextmanager
    def db():
        conn = sqlite3.connect(root / 'state.sqlite3', timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with db() as con:
        con.executescript('''
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS processes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, files TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendente', result TEXT, updated TEXT, active INTEGER DEFAULT 1);
        ''')
        columns = {r['name'] for r in con.execute('PRAGMA table_info(processes)')}
        if 'source_kind' not in columns:
            con.execute("ALTER TABLE processes ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'smb'")
        if 'source_id' not in columns:
            con.execute("ALTER TABLE processes ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
        con.executescript('''
          CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,name TEXT NOT NULL,
            status TEXT NOT NULL,grouping TEXT NOT NULL,expected_files INTEGER NOT NULL,
            expected_bytes INTEGER NOT NULL,created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS upload_files(upload_id TEXT NOT NULL,relative TEXT NOT NULL,
            path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,
            PRIMARY KEY(upload_id,relative),FOREIGN KEY(upload_id) REFERENCES uploads(id));
        ''')
        if 'process_key' not in columns:
            con.execute("ALTER TABLE processes ADD COLUMN process_key TEXT NOT NULL DEFAULT ''")
        for r in con.execute("SELECT id,name FROM processes WHERE source_kind='upload' AND process_key=''").fetchall():
            con.execute('UPDATE processes SET process_key=? WHERE id=?',(unicodedata.normalize('NFC',r['name']).casefold(),r['id']))
        for table,name,definition in [('uploads','selection_key',"TEXT NOT NULL DEFAULT ''"),
             ('uploads','layout_ready','INTEGER NOT NULL DEFAULT 0'),
             ('upload_files','cached','INTEGER NOT NULL DEFAULT 0'),
             ('upload_files','transferred','INTEGER NOT NULL DEFAULT 1'),
             ('upload_files','origin_upload_id',"TEXT NOT NULL DEFAULT ''"),
             ('upload_files','prepare_error',"TEXT NOT NULL DEFAULT ''")]:
            existing={r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
            if name not in existing:con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        con.executescript('''
          CREATE INDEX IF NOT EXISTS upload_files_sha ON upload_files(sha256,size);
          CREATE INDEX IF NOT EXISTS process_key_index ON processes(source_kind,process_key,active);
          CREATE TABLE IF NOT EXISTS pdf_cache(sha256 TEXT PRIMARY KEY,detector TEXT NOT NULL,page_data TEXT NOT NULL,created TEXT NOT NULL,fragment_sha TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS upload_plan(upload_id TEXT NOT NULL,relative TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(upload_id,relative));
          CREATE TABLE IF NOT EXISTS upload_processes(upload_id TEXT NOT NULL,process_id TEXT NOT NULL,PRIMARY KEY(upload_id,process_id));
        ''')
        plan_columns={r['name'] for r in con.execute('PRAGMA table_info(upload_plan)')}
        for name in ('process_name','process_key','process_relative'):
            if name not in plan_columns:con.execute(f"ALTER TABLE upload_plan ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        con.execute('CREATE INDEX IF NOT EXISTS upload_plan_group ON upload_plan(upload_id,process_key)')
        pdf_columns={r['name'] for r in con.execute('PRAGMA table_info(pdf_cache)')}
        if 'fragment_sha' not in pdf_columns:con.execute("ALTER TABLE pdf_cache ADD COLUMN fragment_sha TEXT NOT NULL DEFAULT ''")
        con.execute("UPDATE processes SET status='pendente' WHERE status='processando'")
        con.execute('CREATE INDEX IF NOT EXISTS upload_files_digest ON upload_files(sha256,size,cached)')
        con.execute('CREATE INDEX IF NOT EXISTS upload_files_prepare ON upload_files(upload_id,cached,prepare_error)')
        con.execute('CREATE INDEX IF NOT EXISTS processes_upload_key ON processes(source_kind,process_key,active)')
        con.execute('CREATE INDEX IF NOT EXISTS uploads_selection ON uploads(selection_key,status)')
        con.execute('DELETE FROM settings')
        for old in con.execute("SELECT * FROM processes WHERE source_kind='smb'").fetchall():
            result=json.loads(old['result']) if old['result'] else {}
            preserved=bool(result and result.get('pdf') and (root/'results'/old['id']/'formulario.pdf').exists())
            status=old['status'] if old['status'] not in ('pendente','processando') else 'revisao'
            con.execute("UPDATE processes SET source_kind='legacy',active=?,status=? WHERE id=?",(int(preserved),status,old['id']))
    document_cache=DocumentCache(root,db)
    upload_index=UploadIndex(db)

    def safe_error(exc):
        return explain_error(exc)[1]

    @app.before_request
    def protect():
        if request.method == 'POST' and request.path.startswith('/api/uploads/') and request.path.endswith('/files'):
            request.max_content_length = upload_limits['file_bytes'] + 65536
        if request.path == '/healthz':
            return None
        auth = request.authorization
        if not auth or not (hmac.compare_digest((auth.username or '').encode(), os.environ.get('ADMIN_USER', 'admin').encode())
                            and hmac.compare_digest((auth.password or '').encode(), password.encode())):
            if request.path.startswith('/api/'):
                response = jsonify(error='Autenticação do painel necessária. Recarregue a página e entre novamente.', code='panel_auth_required')
                response.status_code = 401
                response.headers['WWW-Authenticate'] = 'Basic realm="IPTU"'
                return response
            return Response('Autenticação necessária.', 401, {'WWW-Authenticate': 'Basic realm="IPTU"'})
        if request.method == 'POST' and not hmac.compare_digest(
                request.headers.get('X-CSRF-Token', ''), session.get('csrf', 'missing')):
            return jsonify(error='Sessão do painel ausente ou expirada. Recarregue a página. Se estiver acessando por HTTP, configure COOKIE_SECURE=false no Coolify para o teste, ou ative HTTPS mantendo COOKIE_SECURE=true.', code='panel_session_invalid'), 403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.get('/healthz')
    def health():
        return jsonify(ok=True,version='3.0')

    @app.get('/')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return render_template('index.html', csrf=session['csrf'], secure_cookie=app.config['SESSION_COOKIE_SECURE'])

    @app.get('/api/schema.json')
    def data_schema():
        return send_file(Path(__file__).with_name('schema.json'), mimetype='application/schema+json')

    def launch(operation, function):
        with mutex:
            if runtime['running']:
                return jsonify(error='Já existe uma operação em andamento.'), 409
            runtime.update(running=True, operation=operation, current='', done=0, total=0,
                           message='Iniciando…', error=False, error_code=None, diagnostics=[], job_id=secrets.token_hex(8),cancelled=False,details={})
            cancel.clear()
        def target():
            try:
                function()
                if cancel.is_set():
                    runtime.update(message='Operação interrompida. O trabalho concluído foi preservado.',cancelled=True)
            except Exception as exc:
                code, message = explain_error(exc)
                runtime.update(message=message, error=True, error_code=code)
                app.logger.warning('operation_failed operation=%s code=%s exception=%s',operation,code,type(exc).__name__)
            finally:
                runtime.update(running=False, current='')
        threading.Thread(target=target, daemon=True, name='iptu-worker').start()
        return jsonify(ok=True,job_id=runtime['job_id']), 202

    @app.errorhandler(413)
    def upload_too_large(error):
        return jsonify(error='Arquivo ou requisição excedeu o limite de upload. Confira também o limite do proxy no Coolify.'),413

    def upload_row(upload_id):
        with db() as con:
            row=con.execute('SELECT * FROM uploads WHERE id=?',(upload_id,)).fetchone()
        if not row: abort(404)
        return row

    @app.get('/api/uploads')
    def list_uploads():
        with db() as con:
            rows=con.execute("""SELECT u.*,COUNT(f.relative) AS received_files,COALESCE(SUM(f.size),0) AS received_bytes,COALESCE(SUM(CASE WHEN f.transferred=0 THEN 1 ELSE 0 END),0) AS reused_files
              FROM uploads u LEFT JOIN upload_files f ON u.id=f.upload_id GROUP BY u.id ORDER BY u.created DESC LIMIT 30""").fetchall()
        return jsonify(limits=upload_limits,uploads=[dict(r) for r in rows])

    @app.post('/api/uploads')
    def begin_upload():
        data=request.get_json() or {}
        try:
            name=str(data.get('name','Pasta enviada')).strip()[:150]
            count=int(data.get('files',0));size=int(data.get('bytes',0))
            grouping=data.get('grouping','auto')
            if not name or not 1<=count<=upload_limits['files'] or not 0<size<=upload_limits['batch_bytes'] or grouping not in ('auto','single','container'):
                raise ValueError()
        except (ValueError,TypeError):
            return jsonify(error='Confira quantidade, tamanho total e organização da pasta. A seleção excedeu os limites ou está vazia.'),400
        selection_key=data.get('selection_key','')
        if selection_key and (not isinstance(selection_key,str) or not re.fullmatch('[a-f0-9]{64}',selection_key)):
            return jsonify(error='Identificação da seleção inválida.'),400
        if selection_key:
            with db() as con:
                previous=con.execute("SELECT id FROM uploads WHERE status='uploading' AND selection_key=? AND grouping=? AND expected_files=? AND expected_bytes=? ORDER BY created DESC LIMIT 1",(selection_key,grouping,count,size)).fetchone()
            if previous:return jsonify(id=previous['id'],resumed=True),200
        upload_id=secrets.token_hex(16)
        directory=uploads_root/upload_id/'files';directory.mkdir(parents=True)
        with db() as con:
            con.execute('INSERT INTO uploads(id,name,status,grouping,expected_files,expected_bytes,created,selection_key) VALUES(?,?,?,?,?,?,?,?)',(upload_id,name,'uploading',grouping,count,size,datetime.now(timezone.utc).isoformat(),selection_key))
        return jsonify(id=upload_id),201

    @app.post('/api/uploads/<upload_id>/plan')
    def plan_files(upload_id):
        row=upload_row(upload_id)
        if row['status']!='uploading':return jsonify(error='Este envio já foi concluído.'),409
        entries=(request.get_json() or {}).get('files')
        if not isinstance(entries,list) or not 1<=len(entries)<=100:return jsonify(error='Envie até 100 referências por consulta.'),400
        checked=[]
        try:
            for item in entries:
                relative=relative_pdf_path(item.get('relative'))
                size=int(item['size']);sha=item['sha256']
                if not 0<size<=upload_limits['file_bytes'] or not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError()
                checked.append(dict(relative=relative,size=size,sha256=sha))
        except (ValueError,TypeError,KeyError,AttributeError):return jsonify(error='Referência, tamanho ou SHA-256 inválido.'),400
        # Resolve cache presence before opening the write transaction.
        known={item['sha256']:document_cache.get(item['sha256']) is not None for item in checked}
        output=[]
        with upload_lock,db() as con:
            count,total=con.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM upload_plan WHERE upload_id=?',(upload_id,)).fetchone()
            for item in checked:
                relative,size,sha=item['relative'],item['size'],item['sha256']
                previous=con.execute('SELECT * FROM upload_plan WHERE upload_id=? AND relative=?',(upload_id,relative)).fetchone()
                if previous and (previous['size']!=size or previous['sha256']!=sha):
                    con.rollback();return jsonify(error='Um arquivo da seleção mudou. Selecione as pastas novamente.'),409
                if not previous:
                    if count>=row['expected_files'] or total+size>row['expected_bytes']:
                        con.rollback();return jsonify(error='O plano excede a seleção declarada.'),400
                    con.execute('INSERT INTO upload_plan(upload_id,relative,size,sha256) VALUES(?,?,?,?)',(upload_id,relative,size,sha))
                    count+=1;total+=size
                receipt=con.execute('SELECT * FROM upload_files WHERE upload_id=? AND relative=?',(upload_id,relative)).fetchone()
                if receipt and not known[sha]:
                    origin=receipt['origin_upload_id'] or receipt['upload_id']
                    existing_path=uploads_root/origin/'files'/receipt['path']
                    if not existing_path.is_file() or existing_path.stat().st_size!=size:
                        con.execute('DELETE FROM upload_files WHERE upload_id=? AND relative=?',(upload_id,relative));receipt=None
                state='received' if receipt else 'required'
                if not receipt:
                    original=None
                    if not known[sha]:
                        for r in con.execute('SELECT * FROM upload_files WHERE sha256=? AND size=? AND cached=0',(sha,size)).fetchall():
                            origin=r['origin_upload_id'] or r['upload_id']
                            path=uploads_root/origin/'files'/r['path']
                            if path.is_file() and not path.is_symlink():original=r;break
                    if known[sha] or original:
                        origin=(original['origin_upload_id'] or original['upload_id']) if original else upload_id
                        stored=original['path'] if original else hashlib.sha256(relative.encode()).hexdigest()+'.pdf'
                        con.execute('INSERT INTO upload_files(upload_id,relative,path,size,sha256,cached,transferred,origin_upload_id) VALUES(?,?,?,?,?,?,0,?)',
                                    (upload_id,relative,stored,size,sha,int(known[sha]),origin))
                        state='known' if known[sha] else 'received'
                output.append(dict(relative=relative,state=state))
            if count==row['expected_files'] and not row['layout_ready']:
                planned=[dict(r) for r in con.execute('SELECT relative,size,sha256 FROM upload_plan WHERE upload_id=?',(upload_id,))]
                for name,entries in group_items(planned,row['grouping']).items():
                    key=unicodedata.normalize('NFC',name).casefold()
                    con.executemany('UPDATE upload_plan SET process_name=?,process_key=?,process_relative=? WHERE upload_id=? AND relative=?',
                                    [(name,key,e['process_relative'],upload_id,e['relative']) for e in entries])
                con.execute('UPDATE uploads SET layout_ready=1 WHERE id=?',(upload_id,))
        return jsonify(files=output)

    @app.post('/api/uploads/<upload_id>/files')
    def receive_file(upload_id):
        row=upload_row(upload_id)
        if row['status']!='uploading': return jsonify(error='Este envio já foi concluído.'),409
        try:
            relative=relative_pdf_path(request.form.get('path'))
        except ValueError as exc: return jsonify(error=str(exc)),400
        file=request.files.get('file')
        if not file: return jsonify(error='PDF não recebido.'),400
        directory=uploads_root/row['id']/'files'
        temp=directory/(secrets.token_hex(16)+'.part')
        digest=hashlib.sha256();size=0
        try:
            with temp.open('wb') as target:
                while chunk:=file.stream.read(1024*1024):
                    size+=len(chunk)
                    if size>upload_limits['file_bytes']: return jsonify(error='PDF excedeu o limite individual.'),413
                    digest.update(chunk);target.write(chunk)
            with temp.open('rb') as reader:
                if b'%PDF-' not in reader.read(1024): return jsonify(error='O arquivo enviado não é um PDF válido.'),400
            try:
                with fitz.open(temp,filetype='pdf') as document:
                    if not document.is_pdf or len(document)==0: return jsonify(error='PDF vazio ou inválido.'),400
            except Exception:
                return jsonify(error='Não foi possível abrir o PDF enviado. Confira a integridade do arquivo.'),400
            checksum=digest.hexdigest()
            # Serialize quota/commit checks so retries cannot duplicate entries or exceed the batch.
            with upload_lock,db() as con:
                current=con.execute('SELECT status FROM uploads WHERE id=?',(upload_id,)).fetchone()
                if current['status']!='uploading': return jsonify(error='Este envio já foi concluído.'),409
                planned=con.execute('SELECT size,sha256 FROM upload_plan WHERE upload_id=? AND relative=?',(upload_id,relative)).fetchone()
                if planned and (planned['size']!=size or planned['sha256']!=checksum):return jsonify(error='O conteúdo do PDF não corresponde ao SHA-256 informado. Selecione a pasta novamente.'),409
                existing=con.execute('SELECT sha256,size FROM upload_files WHERE upload_id=? AND relative=?',(upload_id,relative)).fetchone()
                if existing:
                    if existing['sha256']==checksum and existing['size']==size: return jsonify(ok=True,repeated=True)
                    return jsonify(error='Este caminho já recebeu um PDF diferente. Inicie um novo envio.'),409
                count,total=con.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM upload_files WHERE upload_id=?',(upload_id,)).fetchone()
                if count>=row['expected_files'] or total+size>row['expected_bytes'] or total+size>upload_limits['batch_bytes']:
                    return jsonify(error='A quantidade ou tamanho recebidos excedem a seleção declarada.'),400
                stored=hashlib.sha256(relative.encode()).hexdigest()+'.pdf'
                cached=document_cache.get(checksum) is not None
                if not cached:temp.replace(directory/stored)
                con.execute('INSERT INTO upload_files(upload_id,relative,path,size,sha256,cached,transferred,origin_upload_id) VALUES(?,?,?,?,?,?,1,?)',
                            (upload_id,relative,stored,size,checksum,int(cached),upload_id))
            return jsonify(ok=True,size=size)
        finally:
            temp.unlink(missing_ok=True)

    @app.post('/api/uploads/<upload_id>/prepare')
    def prepare_documents(upload_id):
        batch=upload_row(upload_id)
        try:
            limit=int((request.get_json() or {}).get('limit',5))
            if not 1<=limit<=50:raise ValueError()
        except (TypeError,ValueError):return jsonify(error='Use de 1 a 50 PDFs por rodada.'),400
        def run():
            with db() as con:
                received=con.execute("SELECT * FROM upload_files WHERE upload_id=? AND cached=0 AND prepare_error='' ORDER BY relative",(upload_id,)).fetchall()
            unique={}
            for row in received:unique.setdefault(row['sha256'],dict(row))
            items=list(unique.values())[:limit]
            runtime['total']=len(items)
            details={'prepared':0,'removed_bytes':0,'failures':[]}
            details['remaining']=max(0,len(unique)-len(items))
            runtime['details']=details
            source=LocalUploadSource(uploads_root,batch['id'])
            for item in items:
                if cancel.is_set():break
                runtime['current']='Preparando '+item['relative']
                try:
                    cached=document_cache.get(item['sha256'])
                    if cached is None:
                        with tempfile.TemporaryDirectory(prefix='iptu-prepare-') as temp:
                            work=Path(temp);original=work/'original.pdf';source.copy(item,original)
                            pages=[]
                            with fitz.open(original) as document:
                                if document.needs_pass:raise ValueError('PDF protegido por senha. Envie uma cópia que possa ser lida.')
                                for i,page in enumerate(document):
                                    if cancel.is_set():raise InterruptedError()
                                    runtime['current']=f"{item['relative']} · página {i+1}/{len(document)}"
                                    _,decision,rotation=page_reader(page,work,str(i))
                                    pages.append(dict(page=i+1,rotation=rotation,**decision))
                            document_cache.store(item['sha256'],pages,original)
                    details['removed_bytes']+=document_cache.purge_originals(item['sha256'])
                    details['prepared']+=1
                except InterruptedError:
                    break
                except Exception as exc:
                    message=safe_error(exc)
                    details['failures'].append({'arquivo':item['relative'],'erro':message})
                    with db() as con:con.execute('UPDATE upload_files SET prepare_error=? WHERE upload_id=? AND relative=?',(message,upload_id,item['relative']))
                    app.logger.warning('pdf_prepare_failed exception=%s',type(exc).__name__)
                runtime['done']+=1
            runtime['message']=f"Rodada preparada: {details['prepared']} PDFs; {len(details['failures'])} falhas. As cópias completas preparadas foram removidas."
        return launch('preparacao',run)

    @app.post('/api/uploads/<upload_id>/checkpoint')
    def release_ready_processes(upload_id):
        with mutex,upload_lock:
            if runtime['running']:return jsonify(error='Aguarde a rodada em andamento.'),409
            batch=upload_row(upload_id)
            with db() as con:
                if not batch['layout_ready']:return jsonify(error='Conclua a conferência de toda a seleção antes de liberar processos.'),409
                names=con.execute("""SELECT p.process_name,p.process_key FROM upload_plan p
                    LEFT JOIN upload_files f ON f.upload_id=p.upload_id AND f.relative=p.relative
                    WHERE p.upload_id=? AND NOT EXISTS(SELECT 1 FROM upload_processes up JOIN processes done ON done.id=up.process_id WHERE up.upload_id=p.upload_id AND done.process_key=p.process_key)
                    GROUP BY p.process_key HAVING COUNT(f.relative)=COUNT(*)""",(upload_id,)).fetchall()
                ready={}
                for group in names:
                    ready[group['process_name']]=[dict(r) for r in con.execute("""SELECT f.relative,f.path,f.size,f.sha256,f.cached,f.origin_upload_id,p.process_relative
                        FROM upload_plan p JOIN upload_files f ON f.upload_id=p.upload_id AND f.relative=p.relative
                        WHERE p.upload_id=? AND p.process_key=? ORDER BY p.relative""",(upload_id,group['process_key']))]
            stats=upload_index.register(batch,ready)
            return jsonify(ok=True,processes=len(ready),**stats)

    @app.post('/api/uploads/<upload_id>/finish')
    def finish_upload(upload_id):
        with mutex,upload_lock:
            if runtime['running']: return jsonify(error='Aguarde o processamento atual terminar antes de concluir o envio.'),409
            row=upload_row(upload_id)
            with db() as con:
                files=[dict(f) for f in con.execute('SELECT relative,path,size,sha256,cached,origin_upload_id FROM upload_files WHERE upload_id=? ORDER BY relative',(upload_id,))]
            if len(files)!=row['expected_files'] or sum(f['size'] for f in files)!=row['expected_bytes']:
                return jsonify(error='O envio ainda está incompleto. Nenhum processo foi liberado para OCR.'),409
            items=group_items(files,row['grouping'])
            stats=upload_index.register(row,items)
            with db() as con: con.execute("UPDATE uploads SET status='ready' WHERE id=?",(row['id'],))
            return jsonify(ok=True,processes=len(items),files=len(files),**stats)

    def process_one(row, source):
        pid = row['id']
        output = root / 'results' / pid
        output.mkdir(exist_ok=True)
        previous=json.loads(row['result']) if row['result'] else None
        if previous and previous.get('pdf') and not previous.get('failures') and (output/'formulario.pdf').exists() and (output/'manifesto.json').exists():
            checkpoint=json.loads((output/'manifesto.json').read_text())
            if checkpoint.get('source_fingerprint')==row['fingerprint']:
                return checkpoint['status'],previous
        removed_bytes=0
        # Archive prior data/reviews before an explicit re-extraction.
        if (output / 'dados.json').exists() or (output / 'dados.revisado.json').exists():
            history = output / 'history' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(4))
            history.mkdir(parents=True,exist_ok=True)
            for artifact in ('formulario.pdf','manifesto.json','dados.json','dados.revisado.json','ocr.txt','formulario_ocr.pdf'):
                if (output / artifact).exists(): shutil.move(str(output / artifact),str(history / artifact))
        # Remove stale preliminary results; originals are never modified.
        for name in ('formulario.pdf', 'manifesto.json', 'dados.json', 'dados.revisado.json', 'ocr.txt', 'formulario_ocr.pdf'):
            (output / name).unlink(missing_ok=True)
        pages, failures = [], []
        files = json.loads(row['files'])
        with tempfile.TemporaryDirectory(prefix='iptu-') as temp:
            work = Path(temp)
            local = []
            for index, item in enumerate(files):
                if cancel.is_set():
                    raise InterruptedError()
                destination = work / f'source-{index}.pdf'
                try:
                    runtime['current'] = f"{row['name']} · PDF {index+1}/{len(files)}"
                    cached=document_cache.get(item.get('sha256')) if row['source_kind']=='upload' else None
                    if cached:
                        local.append(None)
                        pages.extend(dict(**p,file_index=index,source=item['relative'],sha256=item['sha256'],cache_pdf=str(cached['path'])) for p in cached['pages'])
                        removed_bytes+=document_cache.purge_originals(item['sha256'])
                        continue
                    start=len(pages)
                    source.copy(item, destination)
                    local.append(destination)
                    with fitz.open(destination) as document:
                        if document.needs_pass:
                            raise ValueError('PDF protegido por senha.')
                        for page_index, page in enumerate(document):
                            if cancel.is_set():
                                raise InterruptedError()
                            runtime['current'] = f"{row['name']} · PDF {index+1}/{len(files)} · página {page_index+1}/{len(document)}"
                            _, decision, rotation = page_reader(page, work, f'{index}-{page_index}')
                            pages.append(dict(file_index=index, source=item['relative'], page=page_index+1,
                                              rotation=rotation, **decision))
                    if row['source_kind']=='upload':
                        cached=document_cache.store(item['sha256'],pages[start:],destination)
                        pages[start:]=[dict(**p,file_index=index,source=item['relative'],sha256=item['sha256'],cache_pdf=str(cached['path'])) for p in cached['pages']]
                        removed_bytes+=document_cache.purge_originals(item['sha256'])
                except InterruptedError:
                    raise
                except Exception as exc:
                    if len(local) <= index:
                        local.append(None)
                    failures.append(dict(source=item['relative'], error=safe_error(exc)))
            status, selected = assemble(pages)
            if failures:
                status = 'revisao' if selected else 'erro'
            # Retain uncertain candidate pages too, in a separate review section of the PDF.
            uncertain = [p for p in pages if p['candidate'] and not p['roles']]
            export = selected + uncertain
            if export:
                with fitz.open() as extracted:
                    for record in export:
                        with fitz.open(record.get('cache_pdf') or local[record['file_index']]) as original:
                            i = record['cache_page'] if 'cache_pdf' in record else record['page'] - 1
                            extracted.insert_pdf(original, from_page=i, to_page=i)
                            dest = extracted[-1]
                            dest.set_rotation((dest.rotation + record['rotation']) % 360)
                    extracted.save(output / 'formulario.pdf', deflate=True)
            manifest = dict(process=row['name'], status=status, source_kind=row['source_kind'], source_id=row['source_id'], created=datetime.now(timezone.utc).isoformat(),
                classifier=DETECTOR_VERSION, source_fingerprint=row['fingerprint'], definitive_ocr=False,
                warning='Identificação automática preliminar. Validar modelos e completude no piloto.',
                sources=files, pages=pages, selected=export, failures=failures)
            atomic = output / 'manifesto.tmp'
            atomic.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            atomic.replace(output / 'manifesto.json')
        return status, dict(pages=len(export), pdf=bool(export), manifest=True, failures=len(failures),originals_removed=row['source_kind']=='upload' and not failures,original_bytes_removed=removed_bytes)

    def generate_data(row, result, dpi=300):
        output = root / 'results' / row['id']
        manifest = json.loads((output / 'manifesto.json').read_text())
        cache_key=document_cache.ocr_key(manifest,dpi)
        document=document_cache.restore_ocr(cache_key,manifest,output)
        if document is None:
            document = extract_form(output / 'formulario.pdf', manifest, output, dpi=dpi,
            cancelled=cancel.is_set,page_cache=document_cache,
            progress=lambda n,total: runtime.update(current=f"{row['name']} · OCR completo · página {n}/{total}"))
            document_cache.store_ocr(cache_key,output)
        # A newly generated OCR invalidates previous human revisions only on explicit reprocessing.
        (output / 'dados.revisado.json').unlink(missing_ok=True)
        result = {**(result or {}), 'data': True, 'ocr_status': 'revisao_necessaria',
                  'fields': document['resumo']['preenchidos'], 'dpi': dpi}
        return result

    @app.post('/api/run')
    def run_batch():
        try:
            limit = int((request.get_json() or {}).get('limit', 10))
            if not 1 <= limit <= 500:
                raise ValueError()
        except (TypeError, ValueError):
            return jsonify(error='O lote deve ter entre 1 e 500 processos.'), 400
        selected_source=(request.get_json() or {}).get('source','all')
        if selected_source not in ('all','upload','legacy'): return jsonify(error='Origem inválida.'),400
        selected_upload=(request.get_json() or {}).get('upload_id')
        if selected_upload:
            upload=upload_row(selected_upload)
            with db() as con: released=con.execute('SELECT COUNT(*) FROM upload_processes WHERE upload_id=?',(selected_upload,)).fetchone()[0]
            if not released: return jsonify(error='Nenhum processo desta seleção foi liberado ainda.'),409
        auto_data = bool((request.get_json() or {}).get('extract_data', False))
        def run():
            sources={}
            source_failures={}
            with db() as con:
                pending="status='pendente'"
                if auto_data:pending="("+pending+" OR (status NOT IN ('pendente','processando','erro') AND json_valid(result) AND json_extract(result,'$.pdf')=1 AND COALESCE(json_extract(result,'$.data'),0)=0))"
                where="active=1 AND source_kind='upload' AND "+pending
                args=[]
                if selected_source!='all': where+=' AND source_kind=?';args.append(selected_source)
                if selected_upload: where+=" AND id IN (SELECT process_id FROM upload_processes WHERE upload_id=?)";args.append(selected_upload)
                rows=con.execute(f'SELECT * FROM processes WHERE {where} ORDER BY name LIMIT ?',args+[limit]).fetchall()
            runtime['total'] = len(rows)
            for row in rows:
                if cancel.is_set():
                    break
                with db() as con:
                    con.execute("UPDATE processes SET status='processando' WHERE id=?", (row['id'],))
                try:
                    source_key=(row['source_kind'],row['source_id'])
                    if source_key in source_failures:
                        code,message=source_failures[source_key]
                        raise ProcessingDiagnosticError(code,message)
                    if source_key not in sources:
                        try:
                            sources[source_key]=LocalUploadSource(uploads_root,row['source_id'])
                        except Exception as exc:
                            source_failures[source_key]=explain_error(exc)
                            raise
                    status, result = process_one(row, sources[source_key])
                    if auto_data and result.get('pdf'):
                        try:
                            result = generate_data(row, result)
                        except InterruptedError:
                            result.update(ocr_status='pendente')
                        except Exception:
                            result.update(ocr_status='erro', ocr_error='Falha no OCR completo. A extração das páginas foi preservada.')
                except InterruptedError:
                    status, result = 'pendente', None
                except Exception as exc:
                    status, result = 'erro', dict(error=safe_error(exc))
                with db() as con:
                    con.execute('UPDATE processes SET status=?,result=?,updated=? WHERE id=?',
                                (status, json.dumps(result), datetime.now(timezone.utc).isoformat(), row['id']))
                runtime['done'] += 1
            runtime['message'] = f"Lote finalizado: {runtime['done']} processos tratados."
        return launch('lote', run)

    @app.post('/api/ocr')
    def full_ocr():
        data = request.get_json() or {}
        try:
            limit = int(data.get('limit', 10))
            dpi = int(data.get('dpi', 300))
            if not 1 <= limit <= 500 or dpi not in (300, 400):
                raise ValueError()
        except (ValueError, TypeError):
            return jsonify(error='Use lote de 1 a 500 e resolução de 300 ou 400 dpi.'), 400
        selected_source=data.get('source','all')
        if selected_source not in ('all','upload','legacy'): return jsonify(error='Origem inválida.'),400
        pid = data.get('process_id')
        def run():
            with db() as con:
                rows = con.execute("SELECT * FROM processes WHERE active=1 AND status NOT IN ('pendente','processando') ORDER BY name").fetchall()
            queue = []
            for row in rows:
                if selected_source!='all' and row['source_kind']!=selected_source: continue
                result = json.loads(row['result']) if row['result'] else {}
                if not result or not result.get('pdf'):
                    continue
                # Existing JSON, including human review, is never overwritten by a batch.
                if result.get('data'):
                    continue
                if pid and row['id'] != pid:
                    continue
                queue.append(row)
            queue = queue[:limit]
            runtime['total'] = len(queue)
            for row in queue:
                if cancel.is_set(): break
                result = json.loads(row['result'])
                try:
                    result = generate_data(row, result, dpi)
                except InterruptedError:
                    result.update(ocr_status='pendente')
                except Exception:
                    result.update(ocr_status='erro', ocr_error='Falha no OCR completo. Verifique o PDF e tente novamente.')
                with db() as con:
                    con.execute('UPDATE processes SET result=?,updated=? WHERE id=?',
                        (json.dumps(result), datetime.now(timezone.utc).isoformat(),row['id']))
                runtime['done'] += 1
            runtime['message'] = f"OCR finalizado: {runtime['done']} processos tratados. Confira as situações na lista."
        return launch('ocr', run)

    def data_row(pid):
        with db() as con:
            row = con.execute('SELECT * FROM processes WHERE id=? AND active=1', (pid,)).fetchone()
        if not row or row['status'] in ('pendente','processando'):
            abort(404)
        return row

    def read_data(pid):
        row = data_row(pid)
        directory = root / 'results' / row['id']
        revised = directory / 'dados.revisado.json'
        path = revised if revised.exists() else directory / 'dados.json'
        if not path.exists(): abort(404)
        return row, json.loads(path.read_text())

    @app.get('/review/<pid>')
    def review_page(pid):
        row, document = read_data(pid)
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return render_template('review.html', csrf=session['csrf'], pid=pid, process=row['name'], secure_cookie=app.config['SESSION_COOKIE_SECURE'])

    @app.get('/api/data/<pid>')
    def get_data(pid):
        _, document = read_data(pid)
        return jsonify(document)

    @app.post('/api/data/<pid>/review')
    def review_data(pid):
        with mutex:
            if runtime['running']:
                return jsonify(error='Aguarde a operação terminar antes de revisar.'),409
            row, document = read_data(pid)
            try:
                updated = apply_review(document, (request.get_json() or {}).get('changes'), request.authorization.username)
            except ValueError as exc:
                return jsonify(error=str(exc)),400
            directory = root / 'results' / row['id']
            temp = directory / 'review.tmp.json'
            temp.write_text(json.dumps(updated,ensure_ascii=False,indent=2))
            temp.replace(directory / 'dados.revisado.json')
            result = json.loads(row['result'])
            result.update(ocr_status=updated['status'],fields=updated['resumo']['preenchidos'])
            with db() as con:
                con.execute('UPDATE processes SET result=? WHERE id=?',(json.dumps(result),pid))
            return jsonify(ok=True, status=updated['status'])

    @app.get('/api/page/<pid>/<int:number>.png')
    def preview_page(pid, number):
        row = data_row(pid)
        path = root / 'results' / row['id'] / 'formulario.pdf'
        if not path.exists(): abort(404)
        with fitz.open(path) as document:
            if not 1 <= number <= len(document): abort(404)
            image = document[number-1].get_pixmap(dpi=125).tobytes('png')
        return Response(image,mimetype='image/png')

    @app.get('/api/export.jsonl')
    def export_jsonl():
        # Snapshot export of latest available JSON; PDFs/OCR text are not embedded.
        with db() as con:
            ids = [r['id'] for r in con.execute("SELECT id FROM processes WHERE active=1 AND status NOT IN ('pendente','processando') ORDER BY name")]
        def stream():
            for pid in ids:
                directory = root / 'results' / pid
                revised = directory / 'dados.revisado.json'
                path = revised if revised.exists() else directory / 'dados.json'
                if path.exists():
                    try: yield json.dumps(json.loads(path.read_text()),ensure_ascii=False)+'\n'
                    except (OSError,json.JSONDecodeError): continue
        return Response(stream(),mimetype='application/x-ndjson',headers={'Content-Disposition':'attachment; filename=dados-processos.jsonl'})

    @app.post('/api/stop')
    def stop():
        requested=(request.get_json(silent=True) or {}).get('job_id')
        if requested and requested!=runtime.get('job_id'):return jsonify(error='A operação solicitada já terminou.'),409
        cancel.set()
        return jsonify(ok=True)

    @app.post('/api/retry/<pid>')
    def retry(pid):
        with db() as con: target=con.execute('SELECT source_kind FROM processes WHERE id=?',(pid,)).fetchone()
        if target and target['source_kind']!='upload':return jsonify(error='Para refazer este resultado antigo, envie os PDFs pelo navegador.'),400
        with mutex:
            if runtime['running']:
                return jsonify(error='Aguarde a operação terminar.'), 409
            with db() as con:
                con.execute("UPDATE processes SET status='pendente',result=NULL WHERE id=? AND active=1", (pid,))
        return jsonify(ok=True)

    @app.get('/api/state')
    def state():
        snapshot = dict(runtime)
        search = request.args.get('q', '')[:200]
        status = request.args.get('status', '')[:30]
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            page = 1
        selected_source=request.args.get('source','all')
        if selected_source not in ('all','upload','legacy'): return jsonify(error='Origem inválida.'),400
        base_where,base_args='active=1',[]
        if selected_source!='all': base_where+=' AND source_kind=?';base_args.append(selected_source)
        where,args=base_where,list(base_args)
        if search:
            where += ' AND instr(lower(name),lower(?))>0'
            args.append(search)
        if status:
            where += ' AND status=?'
            args.append(status)
        with db() as con:
            counts = dict(con.execute(f'SELECT status,COUNT(*) FROM processes WHERE {base_where} GROUP BY status',base_args).fetchall())
            total = con.execute(f'SELECT COUNT(*) FROM processes WHERE {where}', args).fetchone()[0]
            rows = con.execute(f'SELECT id,name,status,result,updated,files,source_kind,source_id FROM processes WHERE {where} ORDER BY name LIMIT 50 OFFSET ?', args + [(page-1)*50]).fetchall()
        records = []
        for row in rows:
            r = dict(row)
            r['file_count'] = len(json.loads(r.pop('files')))
            r['result'] = json.loads(r['result']) if r['result'] else None
            records.append(r)
        return jsonify(runtime=snapshot, counts=counts, rows=records, total=total, page=page)

    @app.get('/api/export.csv')
    def export_csv():
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(['processo', 'status', 'pdfs_origem', 'paginas_extraidas', 'atualizado'])
        with db() as con:
            for row in con.execute('SELECT * FROM processes WHERE active=1 ORDER BY name'):
                result = json.loads(row['result']) if row['result'] else {}
                name = row['name']
                if name.lstrip().startswith(('=', '+', '-', '@')):
                    name = "'" + name
                writer.writerow([name, row['status'], len(json.loads(row['files'])), (result or {}).get('pages', 0), row['updated'] or ''])
        return Response('\ufeff' + stream.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=processos.csv'})

    @app.get('/results/<pid>/<kind>')
    def download(pid, kind):
        names = {'pdf': 'formulario.pdf', 'json': 'manifesto.json', 'dados': 'dados.json',
                 'bruto': 'dados.json', 'txt': 'ocr.txt', 'ocr': 'formulario_ocr.pdf'}
        if kind not in names:
            abort(404)
        with db() as con:
            row = con.execute('SELECT * FROM processes WHERE id=? AND active=1', (pid,)).fetchone()
        if not row or row['status'] in ('processando', 'pendente'):
            abort(404)
        path = root / 'results' / row['id'] / names[kind]
        if kind == 'dados' and (path.parent / 'dados.revisado.json').exists():
            path = path.parent / 'dados.revisado.json'
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=row['name'] + '-' + names[kind])

    app.extensions['iptu'] = dict(db=db, runtime=runtime, root=root)
    return app
