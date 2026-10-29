"""Durable PDF receipts retain selected form pages and OCR, never a full original."""
import hashlib
import json
import re
import secrets
import shutil
from datetime import datetime,timezone
from pathlib import Path
import pymupdf as fitz

DETECTOR_VERSION='rules-v1-pilot'
PIPELINE_VERSION='forms-json-v2'


class DocumentCache:
    def __init__(self,root,db):
        self.root=Path(root)/'cache';self.root.mkdir(exist_ok=True)
        self.uploads=Path(root)/'uploads';self.db=db

    def get(self,sha):
        if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):return None
        with self.db() as con:
            row=con.execute('SELECT * FROM pdf_cache WHERE sha256=? AND detector=?',(sha,DETECTOR_VERSION)).fetchone()
        if not row:return None
        try:pages=json.loads(row['page_data'])
        except (ValueError,TypeError):return None
        path=self.root/sha/'formulario.pdf'
        if any(p.get('cache_page') is not None for p in pages) and not path.is_file():return None
        if row['fragment_sha'] and path.is_file() and self.file_hash(path)!=row['fragment_sha']:return None
        return {'pages':pages,'path':path}

    @staticmethod
    def file_hash(path):
        digest=hashlib.sha256()
        with path.open('rb') as source:
            while chunk:=source.read(1024*1024):digest.update(chunk)
        return digest.hexdigest()

    def store(self,sha,pages,source_pdf):
        directory=self.root/sha;directory.mkdir(exist_ok=True)
        cleaned=[]
        with fitz.open(source_pdf) as source,fitz.open() as fragment:
            for page in pages:
                item={key:page[key] for key in ('page','rotation','roles','candidate','evidence')}
                item['cache_page']=None
                if page['roles'] or page['candidate']:
                    item['cache_page']=len(fragment)
                    fragment.insert_pdf(source,from_page=page['page']-1,to_page=page['page']-1)
                cleaned.append(item)
            if len(fragment):
                temp=directory/(secrets.token_hex(8)+'.pdf');fragment.save(temp,deflate=True);temp.replace(directory/'formulario.pdf')
        with self.db() as con:
            con.execute('INSERT OR REPLACE INTO pdf_cache(sha256,detector,page_data,created,fragment_sha) VALUES(?,?,?,?,?)',
                        (sha,DETECTOR_VERSION,json.dumps(cleaned),datetime.now(timezone.utc).isoformat(),self.file_hash(directory/'formulario.pdf') if any(p['cache_page'] is not None for p in cleaned) else ''))
        return self.get(sha)

    def purge_originals(self,sha):
        if not self.get(sha):return 0
        removed=0
        with self.db() as con:
            rows=con.execute('SELECT * FROM upload_files WHERE sha256=? AND cached=0',(sha,)).fetchall()
            for row in rows:
                origin=row['origin_upload_id'] or row['upload_id']
                path=self.uploads/origin/'files'/row['path']
                if not re.fullmatch('[a-f0-9]{32}',origin) or not re.fullmatch(r'[a-f0-9]{64}\.pdf',row['path']):continue
                if path.is_symlink():continue
                try:
                    if path.exists():removed+=path.stat().st_size;path.unlink()
                    con.execute('UPDATE upload_files SET cached=1 WHERE upload_id=? AND relative=?',(row['upload_id'],row['relative']))
                except OSError:
                    # Keep the receipt available for a later cleanup, without failing the OCR.
                    continue
        return removed

    def ocr_key(self,manifest,dpi):
        selected=manifest.get('selected',[])
        if not selected or not all(p.get('sha256') for p in selected):return None
        return hashlib.sha256(json.dumps([PIPELINE_VERSION,dpi,[(p['sha256'],p['page'],p['rotation']) for p in selected]],sort_keys=True).encode()).hexdigest()

    def restore_ocr(self,key,manifest,output):
        if not key:return None
        directory=self.root/'ocr'/key
        paths=['dados.json','ocr.txt','formulario_ocr.pdf']
        if not all((directory/p).is_file() for p in paths):return None
        try:data=json.loads((directory/'dados.json').read_text())
        except (ValueError,OSError):return None
        data['processo']=manifest['process'];data['origem_preprocessamento']=manifest['status']
        data['gerado_em']=datetime.now(timezone.utc).isoformat();data['ocr_reaproveitado']=True
        origins=manifest['selected']
        for page in data['paginas']:page['origem']=origins[page['pagina']-1]
        for field in data['campos'].values():
            for e in field['evidencias']+field.get('sugestoes',[]):
                if 1<=e.get('pagina_extraida',0)<=len(origins):e['origem']=origins[e['pagina_extraida']-1]
        for name in ('ocr.txt','formulario_ocr.pdf'):shutil.copyfile(directory/name,output/name)
        temp=output/'dados.tmp.json';temp.write_text(json.dumps(data,ensure_ascii=False,indent=2));temp.replace(output/'dados.json')
        return data

    def store_ocr(self,key,output):
        if not key:return
        directory=self.root/'ocr'/key;directory.mkdir(parents=True,exist_ok=True)
        for name in ('ocr.txt','formulario_ocr.pdf','dados.json'):
            temp=directory/(secrets.token_hex(8)+'.tmp');shutil.copyfile(output/name,temp);temp.replace(directory/name)


    def page_key(self,origin,dpi):
        if not origin.get('sha256'):return None
        return hashlib.sha256(json.dumps([PIPELINE_VERSION,origin['sha256'],origin['page'],origin.get('rotation',0),dpi]).encode()).hexdigest()

    def get_page_ocr(self,origin,dpi):
        key=self.page_key(origin,dpi)
        if not key:return None
        directory=self.root/'pages'/key
        if not (directory/'tokens.json').is_file() or not (directory/'ocr.pdf').is_file():return None
        try:data=json.loads((directory/'tokens.json').read_text())
        except (ValueError,OSError):return None
        data['pdf']=directory/'ocr.pdf'
        return data

    def store_page_ocr(self,origin,dpi,ocr):
        key=self.page_key(origin,dpi)
        if not key:return
        directory=self.root/'pages'/key;directory.mkdir(parents=True,exist_ok=True)
        temp=directory/(secrets.token_hex(8)+'.tmp');shutil.copyfile(ocr['pdf'],temp);temp.replace(directory/'ocr.pdf')
        data={k:v for k,v in ocr.items() if k!='pdf'}
        temp=directory/(secrets.token_hex(8)+'.tmp');temp.write_text(json.dumps(data,ensure_ascii=False));temp.replace(directory/'tokens.json')
