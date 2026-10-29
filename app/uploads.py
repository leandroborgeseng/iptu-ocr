"""Folder uploads retain logical paths; local source never opens a network share."""
import hashlib
import re
from pathlib import Path,PurePosixPath


def relative_pdf_path(value):
    if not isinstance(value,str) or not value or len(value)>1000 or '\\' in value or ':' in value or any(ord(c)<32 for c in value):
        raise ValueError('Caminho de upload inválido.')
    parts=value.split('/')
    if value.startswith('/') or any(p in ('','.','..') for p in parts):
        raise ValueError('Caminho de upload inválido.')
    if not parts[-1].lower().endswith('.pdf'):
        raise ValueError('Somente arquivos PDF são aceitos.')
    return str(PurePosixPath(value))


def group_items(files,grouping='auto'):
    result={}
    direct_roots={PurePosixPath(e['relative']).parts[0] for e in files if len(PurePosixPath(e['relative']).parts)==2}
    for entry in files:
        parts=PurePosixPath(entry['relative']).parts
        root_is_process=bool(re.match(r'^\d{7}\.\d{3}\.\d+',parts[0])) or parts[0] in direct_roots
        if len(parts)==1: process=PurePosixPath(parts[0]).stem
        elif grouping=='single' or (grouping=='auto' and root_is_process): process=parts[0]
        elif len(parts)>=3: process=parts[1]
        else: process=parts[0]
        consumed=0 if len(parts)==1 else 1 if grouping=='single' or (grouping=='auto' and root_is_process) or len(parts)==2 else 2
        item={**entry,'process_relative':'/'.join(parts[consumed:])}
        result.setdefault(process,[]).append(item)
    return result


class LocalUploadSource:
    def __init__(self,uploads_root,upload_id):
        if not re.fullmatch(r'[a-f0-9]{32}',upload_id): raise ValueError('Identificação de upload inválida.')
        self.root=Path(uploads_root)/upload_id

    def copy(self,item,destination):
        name=item['path']
        if not re.fullmatch(r'[a-f0-9]{64}\.pdf',name): raise ValueError('Referência de arquivo enviado inválida.')
        origin=item.get('origin_upload_id')
        source_root=self.root
        if origin:
            if not re.fullmatch(r'[a-f0-9]{32}',origin):raise ValueError('Origem de upload inválida.')
            source_root=self.root.parent/origin
        path=source_root/'files'/name
        if path.is_symlink() or path.resolve().parent!=(source_root/'files').resolve():
            raise ValueError('Referência de arquivo enviado inválida.')
        if not path.exists():raise ValueError('Original já removido. Reenvie o PDF se os formulários preservados não estiverem disponíveis.')
        digest=hashlib.sha256()
        with path.open('rb') as source,open(destination,'wb') as target:
            while chunk:=source.read(1024*1024): digest.update(chunk);target.write(chunk)
        if destination.stat().st_size!=item['size'] or digest.hexdigest()!=item['sha256']:
            path.unlink(missing_ok=True)
            raise ValueError('O PDF enviado foi alterado ou está incompleto. Envie uma nova cópia.')
