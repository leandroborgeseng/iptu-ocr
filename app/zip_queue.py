"""Durable, chunked ZIP intake and a single server-owned background queue."""
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import unicodedata
import zipfile
from datetime import datetime,timezone
from pathlib import Path
import pymupdf as fitz
from .uploads import relative_pdf_path,group_items


def now():return datetime.now(timezone.utc).isoformat()


class ArchiveError(ValueError):pass


class ArchiveQueue:
    chunk_bytes=8*1024*1024

    def __init__(self,root,db,mutex,runtime,cancel,cache,limits,prepare,process,describe_error,logger):
        self.root=Path(root);self.directory=self.root/'archives';self.directory.mkdir(exist_ok=True)
        self.db=db;self.mutex=mutex;self.runtime=runtime;self.cancel=cancel;self.cache=cache
        self.limits=limits;self.prepare=prepare;self.process=process;self.describe_error=describe_error;self.logger=logger
        self.wake=threading.Event();self.stopping=threading.Event();self.thread=None
        def limit(name,default):
            try:
                value=int(os.environ.get(name,str(default)))
                if value<1:raise ValueError()
                return value*1024*1024
            except ValueError:
                logger.warning('%s inválido; usando o padrão.',name);return default*1024*1024
        self.max_bytes=limit('ZIP_MAX_FILE_MB',65536)
        self.max_stored=limit('ZIP_MAX_STORED_MB',65536)
        with db() as con:
            con.executescript('''
              CREATE TABLE IF NOT EXISTS archive_control(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL DEFAULT 0);
              INSERT OR IGNORE INTO archive_control(id,paused) VALUES(1,0);
              CREATE TABLE IF NOT EXISTS archives(id TEXT PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,
                size INTEGER NOT NULL,received INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,upload_id TEXT NOT NULL,
                grouping TEXT NOT NULL,extract_data INTEGER NOT NULL,validated INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL,updated TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS archive_members(archive_id TEXT NOT NULL,relative TEXT NOT NULL,member_index INTEGER NOT NULL,
                size INTEGER NOT NULL,process_key TEXT NOT NULL,process_relative TEXT NOT NULL,sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',PRIMARY KEY(archive_id,relative));
              CREATE TABLE IF NOT EXISTS archive_groups(archive_id TEXT NOT NULL,process_key TEXT NOT NULL,name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',process_id TEXT,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(archive_id,process_key));
              CREATE INDEX IF NOT EXISTS archives_fifo ON archives(status,created);
            ''')
            columns={row['name'] for row in con.execute('PRAGMA table_info(archives)')}
            if 'validated' not in columns:con.execute('ALTER TABLE archives ADD COLUMN validated INTEGER NOT NULL DEFAULT 0')
            con.execute("UPDATE archives SET status='queued',updated=? WHERE status='processing'",(now(),))
            completed=[r['id'] for r in con.execute("SELECT id FROM archives WHERE status='done'")]
        for identifier in completed:
            try:
                for suffix in ('.zip','.part'):(self.directory/(identifier+suffix)).unlink(missing_ok=True)
            except OSError:logger.warning('archive_cleanup_deferred')

    def get(self,identifier):
        if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier):raise ArchiveError('Pacote ZIP inválido.')
        with self.db() as con:row=con.execute('SELECT * FROM archives WHERE id=?',(identifier,)).fetchone()
        if row is None:raise ArchiveError('Pacote ZIP não encontrado.')
        return dict(row)

    def body(self,job):
        complete=self.directory/(job['id']+'.zip')
        return complete if complete.exists() else self.directory/(job['id']+'.part')

    def begin(self,data):
        name=data.get('name');sha=data.get('sha256');grouping=data.get('grouping','auto')
        try:size=int(data.get('size',0))
        except (ValueError,TypeError):raise ArchiveError('Tamanho de ZIP inválido.')
        if not isinstance(name,str) or not name.lower().endswith('.zip') or len(name)>250 or any(c in name for c in '/\\:') or any(ord(c)<32 for c in name):raise ArchiveError('Selecione um arquivo .zip com nome válido.')
        if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ArchiveError('SHA-256 do ZIP inválido.')
        if not 0<size<=self.max_bytes:raise ArchiveError('O ZIP excede o limite por pacote exibido no painel.')
        if grouping not in ('auto','single','container'):raise ArchiveError('Agrupamento de ZIP inválido.')
        with self.db() as con:
            old=con.execute('SELECT * FROM archives WHERE sha256=?',(sha,)).fetchone()
            if old and old['status']!='cancelled':
                job=dict(old)
                if job['size']!=size:raise ArchiveError('O conteúdo do ZIP não corresponde ao registro anterior.')
                received=self.body(job).stat().st_size if self.body(job).exists() else 0
                return dict(id=job['id'],received=received,status=job['status'],known=job['status']!='receiving',chunk_bytes=self.chunk_bytes)
            reserved=con.execute("SELECT COALESCE(SUM(size),0) FROM archives WHERE status NOT IN ('done','cancelled')").fetchone()[0]
            if reserved+size>self.max_stored:raise ArchiveError('O limite de armazenamento da fila foi atingido. Aguarde a conclusão dos pacotes ou ajuste ZIP_MAX_STORED_MB.')
            if shutil.disk_usage(self.directory).free<size+64*1024*1024:raise ArchiveError('Espaço livre insuficiente para receber este ZIP. Amplie o volume ou aguarde a fila liberar espaço.')
            identifier=old['id'] if old else secrets.token_hex(16)
            if old:
                con.execute('DELETE FROM archive_members WHERE archive_id=?',(identifier,));con.execute('DELETE FROM archive_groups WHERE archive_id=?',(identifier,))
                con.execute('DELETE FROM archives WHERE id=?',(identifier,))
            con.execute('INSERT INTO archives(id,name,sha256,size,status,upload_id,grouping,extract_data,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (identifier,name,sha,size,'receiving',secrets.token_hex(16),grouping,int(bool(data.get('extract_data',True))),now(),now()))
        return dict(id=identifier,received=0,status='receiving',known=False,chunk_bytes=self.chunk_bytes)

    def receive(self,identifier,offset,stream,length):
        job=self.get(identifier)
        if job['status']!='receiving':raise ArchiveError('Este ZIP já saiu da etapa de envio.')
        if (self.directory/(identifier+'.zip')).exists():raise ArchiveError('O ZIP já foi recebido. Confirme o envio para liberá-lo à fila.')
        path=self.directory/(identifier+'.part')
        actual=path.stat().st_size if path.exists() else 0
        if offset!=actual:raise ArchiveError('O envio está em outra posição. Tente novamente para retomar os bytes confirmados.')
        if not length or not 0<length<=self.chunk_bytes or actual+length>job['size']:raise ArchiveError('Bloco de upload inválido.')
        if shutil.disk_usage(self.directory).free<length+64*1024*1024:raise ArchiveError('Sem espaço para receber o próximo bloco do ZIP.')
        try:
            with path.open('ab') as target:
                remaining=length
                while remaining:
                    chunk=stream.read(min(1024*1024,remaining))
                    if not chunk:raise ArchiveError('Bloco incompleto. O próximo envio retomará os bytes recebidos.')
                    target.write(chunk);remaining-=len(chunk)
                target.flush();os.fsync(target.fileno())
        finally:
            received=path.stat().st_size if path.exists() else 0
            with self.db() as con:con.execute('UPDATE archives SET received=?,updated=? WHERE id=?',(received,now(),identifier))
        return dict(received=received,total=job['size'])

    def inspect(self,path):
        entries=[];keys=set();total=0
        try:
            with zipfile.ZipFile(path) as archive:
                infos=archive.infolist()
                if len(infos)>100000:raise ArchiveError('ZIP com entradas demais. Divida em pacotes menores.')
                for index,info in enumerate(infos):
                    name=info.filename.replace('\\','/')
                    if info.is_dir() or '__MACOSX' in name.split('/') or name.split('/')[-1].startswith('._') or not name.lower().endswith('.pdf'):continue
                    try:name=relative_pdf_path(name)
                    except ValueError as exc:raise ArchiveError(str(exc)) from exc
                    key=unicodedata.normalize('NFC',name).casefold()
                    if key in keys:raise ArchiveError('O ZIP contém caminhos PDF duplicados. Reorganize o pacote antes de reenviar.')
                    keys.add(key)
                    if info.flag_bits&1:raise ArchiveError('ZIP protegido por senha não é aceito. Envie sem criptografia.')
                    if stat.S_ISLNK(info.external_attr>>16):raise ArchiveError('Links simbólicos não são aceitos como PDFs no ZIP.')
                    if not 0<info.file_size<=self.limits['file_bytes']:raise ArchiveError('Um PDF do ZIP está vazio ou excede o limite por PDF.')
                    if info.file_size>max(info.compress_size,1)*2000:raise ArchiveError('Taxa de compressão excessiva no ZIP.')
                    total+=info.file_size
                    entries.append(dict(relative=name,member_index=index,size=info.file_size))
                    if len(entries)>self.limits['files'] or total>self.limits['batch_bytes']:raise ArchiveError('PDFs descompactados excedem os limites por seleção. Divida o ZIP.')
        except (zipfile.BadZipFile,NotImplementedError,RuntimeError) as exc:raise ArchiveError('ZIP inválido ou com método de compressão não suportado.') from exc
        if not entries:raise ArchiveError('Nenhum PDF foi encontrado no ZIP.')
        return entries

    def complete(self,identifier):
        job=self.get(identifier)
        if job['status']!='receiving':return dict(id=identifier,status=job['status'],known=True)
        path=self.body(job)
        if not path.exists() or path.stat().st_size!=job['size']:raise ArchiveError('O ZIP ainda não foi recebido por completo.')
        final=self.directory/(identifier+'.zip')
        if path!=final:path.replace(final)
        with self.db() as con:con.execute("UPDATE archives SET status='validating',received=size,error='',updated=? WHERE id=?",(now(),identifier))
        self.wake.set()
        return dict(id=identifier,status='validating')

    def validate_job(self,job):
        identifier=job['id'];path=self.body(job);digest=hashlib.sha256();checked=0
        with path.open('rb') as source:
            while chunk:=source.read(4*1024*1024):
                if self.cancel.is_set():raise InterruptedError()
                digest.update(chunk);checked+=len(chunk)
                self.runtime['current']=f"Validando {job['name']} · {checked//(1024*1024)} / {job['size']//(1024*1024)} MB"
        if checked!=job['size'] or digest.hexdigest()!=job['sha256']:
            path.unlink()
            with self.db() as con:con.execute("UPDATE archives SET status='receiving',received=0,error=? WHERE id=?",('Falha de integridade; selecione o mesmo ZIP para reenviar.',identifier))
            raise ArchiveError('Falha de integridade. Selecione o mesmo ZIP para reenviar desde o início.')
        entries=self.inspect(path)
        flat=all('/' not in e['relative'] for e in entries)
        stem=Path(job['name']).stem
        if flat and (job['grouping']=='single' or re.match(r'^\d{7}\.\d{3}\.\d+',stem)):
            entries=[{**e,'relative':relative_pdf_path(stem+'/'+e['relative'])} for e in entries]
        groups=group_items(entries,job['grouping'])
        with self.db() as con:
            con.execute('INSERT OR IGNORE INTO uploads(id,name,status,grouping,expected_files,expected_bytes,created) VALUES(?,?,?,?,?,?,?)',
                        (job['upload_id'],job['name'],'uploading',job['grouping'],len(entries),sum(e['size'] for e in entries),now()))
            for name,files in groups.items():
                key=unicodedata.normalize('NFC',name).casefold()
                con.execute('INSERT OR IGNORE INTO archive_groups(archive_id,process_key,name) VALUES(?,?,?)',(identifier,key,name))
                con.executemany('INSERT OR IGNORE INTO archive_members(archive_id,relative,member_index,size,process_key,process_relative) VALUES(?,?,?,?,?,?)',
                                [(identifier,e['relative'],e['member_index'],e['size'],key,e['process_relative']) for e in files])
            con.execute('UPDATE archives SET validated=1,error=?,updated=? WHERE id=?',('',now(),identifier))

    def import_member(self,job,member,archive):
        with self.db() as con:receipt=con.execute('SELECT * FROM upload_files WHERE upload_id=? AND relative=?',(job['upload_id'],member['relative'])).fetchone()
        if receipt:
            item=dict(receipt)
            body=self.root/'uploads'/(item['origin_upload_id'] or job['upload_id'])/'files'/item['path']
            if self.cache.get(item['sha256']) is not None or body.is_file():
                with self.db() as con:con.execute("UPDATE archive_members SET status='ready',sha256=?,error='' WHERE archive_id=? AND relative=?",(item['sha256'],job['id'],member['relative']))
                return item
        directory=self.root/'uploads'/job['upload_id']/'files';directory.mkdir(parents=True,exist_ok=True)
        stored=hashlib.sha256(member['relative'].encode()).hexdigest()+'.pdf';temporary=directory/(stored+'.part')
        digest=hashlib.sha256();size=0
        try:
            with archive.open(archive.infolist()[member['member_index']]) as source,temporary.open('wb') as target:
                while chunk:=source.read(1024*1024):
                    if self.cancel.is_set():raise InterruptedError()
                    size+=len(chunk)
                    if size>member['size'] or size>self.limits['file_bytes']:raise ArchiveError('Tamanho descompactado inválido.')
                    digest.update(chunk);target.write(chunk)
            if size!=member['size']:raise ArchiveError('PDF incompleto no ZIP.')
            with temporary.open('rb') as source:
                if not source.read(1024).lstrip().startswith(b'%PDF-'):raise ArchiveError('Um arquivo com extensão PDF não contém um PDF válido.')
            with fitz.open(temporary) as document:
                if not document.is_pdf or document.needs_pass or not len(document):raise ArchiveError('PDF inválido, vazio ou protegido por senha.')
            sha=digest.hexdigest();cached=self.cache.get(sha) is not None
            if cached:temporary.unlink()
            else:temporary.replace(directory/stored)
            with self.db() as con:
                con.execute('INSERT OR REPLACE INTO upload_files(upload_id,relative,path,size,sha256,cached,transferred,origin_upload_id) VALUES(?,?,?,?,?,?,1,?)',
                            (job['upload_id'],member['relative'],stored,size,sha,int(cached),job['upload_id']))
                con.execute("UPDATE archive_members SET status='ready',sha256=?,error='' WHERE archive_id=? AND relative=?",(sha,job['id'],member['relative']))
            return dict(upload_id=job['upload_id'],relative=member['relative'],path=stored,size=size,sha256=sha,cached=int(cached),origin_upload_id=job['upload_id'])
        finally:temporary.unlink(missing_ok=True)

    def run_next(self):
        with self.mutex:
            if self.runtime['running'] or self.stopping.is_set():return False
            with self.db() as con:
                if con.execute('SELECT paused FROM archive_control WHERE id=1').fetchone()[0]:return False
                con.execute("UPDATE archives SET status='queued' WHERE status='processing'")
                row=con.execute("SELECT * FROM archives WHERE status IN ('queued','validating') ORDER BY created,id LIMIT 1").fetchone()
                if not row:return False
                job=dict(row);con.execute("UPDATE archives SET status='processing',updated=? WHERE id=?",(now(),job['id']))
            self.cancel.clear()
            self.runtime.update(running=True,operation='arquivo_zip',current=job['name'],done=0,total=0,error=False,error_code=None,
                                message='Processando a fila de ZIPs no servidor.',job_id=secrets.token_hex(8),cancelled=False,details={'archive_id':job['id'],'archive_name':job['name']})
        try:
            if not job['validated']:self.validate_job(job)
            with self.db() as con:
                groups=con.execute("SELECT * FROM archive_groups WHERE archive_id=? AND status!='done' ORDER BY name",(job['id'],)).fetchall()
            self.runtime['total']=len(groups)
            with zipfile.ZipFile(self.body(job)) as archive:
                for group in groups:
                    if self.cancel.is_set():raise InterruptedError()
                    errors=[];files=[]
                    with self.db() as con:members=con.execute('SELECT * FROM archive_members WHERE archive_id=? AND process_key=? ORDER BY relative',(job['id'],group['process_key'])).fetchall()
                    for member in members:
                        if self.cancel.is_set():raise InterruptedError()
                        self.runtime['current']=job['name']+' · '+member['relative']
                        try:
                            item=self.import_member(job,member,archive)
                            self.prepare(job['upload_id'],item)
                            files.append(dict(**item,process_relative=member['process_relative'],archive_name=job['name'],archive_sha256=job['sha256']))
                        except InterruptedError:raise
                        except Exception as exc:
                            message=self.describe_error(exc);errors.append(message)
                            with self.db() as con:con.execute("UPDATE archive_members SET status='error',error=? WHERE archive_id=? AND relative=?",(message,job['id'],member['relative']))
                    pid=None
                    if not errors:
                        try:
                            pid,error=self.process(job['upload_id'],group['name'],files,bool(job['extract_data']))
                            if error:errors.append(error)
                        except InterruptedError:raise
                        except Exception as exc:errors.append(self.describe_error(exc))
                    with self.db() as con:con.execute('UPDATE archive_groups SET status=?,process_id=?,error=? WHERE archive_id=? AND process_key=?',
                        ('error' if errors else 'done',pid,'; '.join(errors)[:1000],job['id'],group['process_key']))
                    self.runtime['done']+=1
            with self.db() as con:
                failures=con.execute("SELECT COUNT(*) FROM archive_groups WHERE archive_id=? AND status='error'",(job['id'],)).fetchone()[0]
                con.execute('UPDATE archives SET status=?,error=?,updated=? WHERE id=?',('error' if failures else 'done',f'{failures} processo(s) com falha. Os demais foram preservados.' if failures else '',now(),job['id']))
                con.execute("UPDATE uploads SET status='ready' WHERE id=?",(job['upload_id'],))
            if not failures:
                self.body(job).unlink(missing_ok=True)
            self.runtime['message']='ZIP finalizado. Próximo pacote será iniciado automaticamente.' if not failures else 'ZIP com falhas registradas; seguindo para o próximo pacote.'
        except InterruptedError:
            with self.db() as con:
                con.execute("UPDATE archives SET status='queued',updated=? WHERE id=?",(now(),job['id']))
                con.execute('UPDATE archive_control SET paused=1 WHERE id=1')
            self.runtime.update(message='Fila pausada. O trabalho concluído foi preservado.',cancelled=True)
        except Exception as exc:
            with self.db() as con:con.execute("UPDATE archives SET status='error',error=?,updated=? WHERE id=? AND status!='receiving'",(self.describe_error(exc),now(),job['id']))
            self.runtime.update(error=True,message=self.describe_error(exc))
            self.logger.warning('archive_job_failed exception=%s',type(exc).__name__)
        finally:
            with self.mutex:self.runtime.update(running=False,current='')
        return True

    def state(self):
        with self.db() as con:
            paused=bool(con.execute('SELECT paused FROM archive_control WHERE id=1').fetchone()[0])
            rows=[dict(r) for r in con.execute('''SELECT a.*,
                (SELECT COUNT(*) FROM archive_groups g WHERE g.archive_id=a.id) AS processes,
                (SELECT COUNT(*) FROM archive_groups g WHERE g.archive_id=a.id AND g.status='done') AS done,
                (SELECT COUNT(*) FROM archive_groups g WHERE g.archive_id=a.id AND g.status='error') AS failed
                FROM archives a ORDER BY a.created DESC LIMIT 100''')]
            for row in rows:
                row['issues']=[]
                if row['status']=='error':
                    row['issues']=[dict(r) for r in con.execute("SELECT relative AS name,error FROM archive_members WHERE archive_id=? AND status='error' LIMIT 5",(row['id'],))]
                    if not row['issues']:row['issues']=[dict(r) for r in con.execute("SELECT name,error FROM archive_groups WHERE archive_id=? AND status='error' LIMIT 5",(row['id'],))]
            counts=dict(con.execute('SELECT status,COUNT(*) FROM archives GROUP BY status').fetchall())
        return dict(paused=paused,counts=counts,archives=rows,limits=dict(file_bytes=self.max_bytes,stored_bytes=self.max_stored,chunk_bytes=self.chunk_bytes))

    def control(self,action,identifier=None):
        with self.mutex,self.db() as con:
            if action in ('pause','resume'):
                if action=='resume' and self.runtime['running'] and self.runtime['operation']=='arquivo_zip' and self.cancel.is_set():raise ArchiveError('Aguarde a interrupção atual terminar antes de retomar a fila.')
                con.execute('UPDATE archive_control SET paused=? WHERE id=1',(int(action=='pause'),))
                if action=='pause' and self.runtime['running'] and self.runtime['operation']=='arquivo_zip':self.cancel.set()
            elif action=='retry':
                job=self.get(identifier)
                if job['status']!='error' or not self.body(job).is_file():raise ArchiveError('Este pacote não está disponível para nova tentativa.')
                if not con.execute('SELECT 1 FROM archive_groups WHERE archive_id=? LIMIT 1',(identifier,)).fetchone():raise ArchiveError('O ZIP não passou na validação. Descarte-o e envie um novo pacote corrigido.')
                con.execute("UPDATE archive_groups SET status='pending',error='' WHERE archive_id=? AND status='error'",(identifier,))
                con.execute("UPDATE archives SET status='queued',error='',updated=? WHERE id=?",(now(),identifier))
            elif action=='discard':
                job=self.get(identifier)
                if job['status']=='processing':raise ArchiveError('Pause a fila e aguarde antes de descartar este ZIP.')
                for suffix in ('.zip','.part'):(self.directory/(identifier+suffix)).unlink(missing_ok=True)
                con.execute("UPDATE archives SET status='cancelled',updated=? WHERE id=?",(now(),identifier))
            else:raise ArchiveError('Ação de fila inválida.')
        self.wake.set()

    def start(self):
        if self.thread:return
        def loop():
            while not self.stopping.is_set():
                try:
                    if self.run_next():continue
                except Exception as exc:self.logger.warning('archive_scheduler_failed exception=%s',type(exc).__name__)
                self.wake.wait(2);self.wake.clear()
        self.thread=threading.Thread(target=loop,name='iptu-zip-queue',daemon=True);self.thread.start()

    def close(self):
        self.stopping.set();self.wake.set()
        if self.runtime.get('operation')=='arquivo_zip' and self.runtime['running']:self.cancel.set()
        if self.thread:self.thread.join(timeout=5)
