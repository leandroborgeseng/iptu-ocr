import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from flask import Flask, Response, abort, jsonify, render_template, request, send_file, session
import pymupdf as fitz
from .source import SMBSource, validate
from .diagnostics import explain_error
from .detector import read_page, assemble
from .extraction import extract_form, apply_review


def create_app(data_dir=None, source_factory=SMBSource, page_reader=read_page):
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
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
    mutex = threading.Lock()
    cancel = threading.Event()
    runtime = {'running': False, 'operation': '', 'current': '', 'done': 0, 'total': 0,
               'message': 'Pronto para configurar.', 'error': False, 'error_code': None, 'diagnostics': []}

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
        con.execute("UPDATE processes SET status='pendente' WHERE status='processando'")

    def config():
        with db() as con:
            row = con.execute('SELECT value FROM settings WHERE id=1').fetchone()
        return json.loads(cipher.decrypt(row['value'])) if row else {}

    def safe_error(exc):
        return explain_error(exc)[1]

    @app.before_request
    def protect():
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
        return jsonify(ok=True)

    @app.get('/')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return render_template('index.html', csrf=session['csrf'], secure_cookie=app.config['SESSION_COOKIE_SECURE'])

    @app.get('/api/schema.json')
    def data_schema():
        return send_file(Path(__file__).with_name('schema.json'), mimetype='application/schema+json')

    @app.get('/api/config')
    def get_config():
        data = config()
        data['has_password'] = bool(data.pop('password', ''))
        return jsonify(data)

    def launch(operation, function):
        with mutex:
            if runtime['running']:
                return jsonify(error='Já existe uma operação em andamento.'), 409
            runtime.update(running=True, operation=operation, current='', done=0, total=0,
                           message='Iniciando…', error=False, error_code=None, diagnostics=[])
            cancel.clear()
        def target():
            try:
                function()
                if cancel.is_set():
                    runtime.update(message='Operação interrompida. O trabalho concluído foi preservado.')
            except Exception as exc:
                code, message = explain_error(exc)
                runtime.update(message=message, error=True, error_code=code)
                app.logger.warning('operation_failed operation=%s code=%s exception=%s',operation,code,type(exc).__name__)
            finally:
                runtime.update(running=False, current='')
        threading.Thread(target=target, daemon=True, name='iptu-worker').start()
        return jsonify(ok=True), 202

    @app.post('/api/config')
    def save_config():
        with mutex:
            if runtime['running']:
                return jsonify(error='Aguarde a operação terminar antes de alterar a conexão.'), 409
            data = request.get_json() or {}
            old = config()
            if not data.get('password'):
                data['password'] = old.get('password', '')
            try:
                data = validate({key: data.get(key, '') for key in
                    ('server', 'share', 'subpath', 'domain', 'username', 'password', 'stable_seconds')})
            except (ValueError, TypeError):
                return jsonify(error='Confira servidor, compartilhamento, usuário e estabilidade (30–86400 segundos).'), 400
            scope = lambda c: (c.get('server'), c.get('share'), c.get('subpath'))
            with db() as con:
                con.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)',
                            (cipher.encrypt(json.dumps(data).encode()),))
                if scope(old) != scope(data):
                    con.execute('UPDATE processes SET active=0')
            return jsonify(ok=True)

    @app.post('/api/test')
    def test_connection():
        def run():
            connection_config = config()
            steps = runtime['diagnostics']
            def step(label, action):
                entry = {'etapa': label, 'status': 'executando'}
                steps.append(entry)
                runtime['current'] = label
                try:
                    result = action()
                    entry['status'] = 'ok'
                    return result
                except Exception as exc:
                    code, message = explain_error(exc)
                    entry.update(status='erro', codigo=code, mensagem=message)
                    raise
            if callable(getattr(source_factory, 'probe', None)):
                step('Rede e porta TCP 445',lambda: source_factory.probe(connection_config))
            if cancel.is_set(): return
            source = step('Negociação SMB e autenticação Windows',lambda: source_factory(connection_config))
            if cancel.is_set(): return
            names = step('Compartilhamento, subpasta e permissão de listagem',source.test)
            runtime.update(message=f'Conexão confirmada. {len(names)} entradas verificadas (amostra). O teste verifica listagem; cada PDF será lido durante o processamento.')
        return launch('conexao', run)

    def inventory():
        c = config()
        source = source_factory(c)
        runtime['message'] = 'Lendo as pastas do compartilhamento…'
        items = source.inventory()
        if cancel.is_set():
            return
        scope = json.dumps([c.get('server'), c.get('share'), c.get('subpath')])
        with db() as con:
            con.execute('UPDATE processes SET active=0')
            for name, files in sorted(items.items()):
                files = sorted(files, key=lambda f: f['relative'])
                pid = hashlib.sha256((scope + name).encode()).hexdigest()[:24]
                fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
                existing = con.execute('SELECT fingerprint FROM processes WHERE id=?', (pid,)).fetchone()
                if existing and existing['fingerprint'] == fingerprint:
                    con.execute('UPDATE processes SET active=1 WHERE id=?', (pid,))
                else:
                    con.execute('''INSERT INTO processes(id,name,files,fingerprint,status,active)
                      VALUES(?,?,?,?,'pendente',1) ON CONFLICT(id) DO UPDATE SET
                      files=excluded.files,fingerprint=excluded.fingerprint,status='pendente',
                      result=NULL,active=1''', (pid, name, json.dumps(files), fingerprint))
        runtime.update(message=f'Inventário atualizado: {len(items)} processos, '
                       f'{sum(map(len, items.values()))} PDFs.', done=len(items), total=len(items))

    @app.post('/api/scan')
    def scan():
        return launch('inventario', inventory)

    def process_one(row, source):
        pid = row['id']
        output = root / 'results' / pid
        output.mkdir(exist_ok=True)
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
                        with fitz.open(local[record['file_index']]) as original:
                            i = record['page'] - 1
                            extracted.insert_pdf(original, from_page=i, to_page=i)
                            dest = extracted[-1]
                            dest.set_rotation((dest.rotation + record['rotation']) % 360)
                    extracted.save(output / 'formulario.pdf', deflate=True)
            manifest = dict(process=row['name'], status=status, created=datetime.now(timezone.utc).isoformat(),
                classifier='rules-v1-pilot', definitive_ocr=False,
                warning='Identificação automática preliminar. Validar modelos e completude no piloto.',
                sources=files, pages=pages, selected=export, failures=failures)
            atomic = output / 'manifesto.tmp'
            atomic.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            atomic.replace(output / 'manifesto.json')
        return status, dict(pages=len(export), pdf=bool(export), manifest=True, failures=len(failures))

    def generate_data(row, result, dpi=300):
        output = root / 'results' / row['id']
        manifest = json.loads((output / 'manifesto.json').read_text())
        document = extract_form(output / 'formulario.pdf', manifest, output, dpi=dpi,
            cancelled=cancel.is_set,
            progress=lambda n,total: runtime.update(current=f"{row['name']} · OCR completo · página {n}/{total}"))
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
        auto_data = bool((request.get_json() or {}).get('extract_data', False))
        def run():
            source = source_factory(config())
            with db() as con:
                rows = con.execute("SELECT * FROM processes WHERE active=1 AND status='pendente' ORDER BY name LIMIT ?", (limit,)).fetchall()
            runtime['total'] = len(rows)
            for row in rows:
                if cancel.is_set():
                    break
                with db() as con:
                    con.execute("UPDATE processes SET status='processando' WHERE id=?", (row['id'],))
                try:
                    status, result = process_one(row, source)
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
        pid = data.get('process_id')
        def run():
            with db() as con:
                rows = con.execute("SELECT * FROM processes WHERE active=1 AND status NOT IN ('pendente','processando') ORDER BY name").fetchall()
            queue = []
            for row in rows:
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
        cancel.set()
        return jsonify(ok=True)

    @app.post('/api/retry/<pid>')
    def retry(pid):
        with mutex:
            if runtime['running']:
                return jsonify(error='Aguarde a operação terminar.'), 409
            with db() as con:
                con.execute("UPDATE processes SET status='pendente' WHERE id=? AND active=1", (pid,))
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
        where, args = 'active=1', []
        if search:
            where += ' AND instr(lower(name),lower(?))>0'
            args.append(search)
        if status:
            where += ' AND status=?'
            args.append(status)
        with db() as con:
            counts = dict(con.execute('SELECT status,COUNT(*) FROM processes WHERE active=1 GROUP BY status').fetchall())
            total = con.execute(f'SELECT COUNT(*) FROM processes WHERE {where}', args).fetchone()[0]
            rows = con.execute(f'SELECT id,name,status,result,updated,files FROM processes WHERE {where} ORDER BY name LIMIT 50 OFFSET ?', args + [(page-1)*50]).fetchall()
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
