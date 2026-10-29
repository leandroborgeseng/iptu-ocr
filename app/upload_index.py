"""Content-based process identities survive new uploads, renamed roots and restarts."""
import hashlib
import json
import unicodedata
from .document_cache import PIPELINE_VERSION,DETECTOR_VERSION


class UploadIndex:
    def __init__(self,db):self.db=db

    def register(self,batch,groups):
        stats=dict(new=0,updated=0,unchanged=0,resumed=0)
        for name,incoming in groups.items():
            key=unicodedata.normalize('NFC',name).casefold()
            with self.db() as con:
                old=con.execute("SELECT * FROM processes WHERE source_kind='upload' AND process_key=? AND active=1 ORDER BY updated DESC LIMIT 1",(key,)).fetchone()
            merged={}
            if old:
                for item in json.loads(old['files']):merged[unicodedata.normalize('NFC',item.get('process_relative',item['relative'])).casefold()]=item
            for item in incoming:merged[unicodedata.normalize('NFC',item['process_relative']).casefold()]=item
            unique={}
            for item in merged.values():
                sha=item['sha256']
                if sha not in unique:unique[sha]=dict(item)
                else:unique[sha].setdefault('duplicate_paths',[]).append(item['relative'])
            files=sorted(unique.values(),key=lambda item:item.get('process_relative',item['relative']))
            fingerprint=hashlib.sha256(json.dumps([PIPELINE_VERSION,DETECTOR_VERSION,key,sorted(unique)],sort_keys=True).encode()).hexdigest()
            pid=hashlib.sha256(('upload-process:'+fingerprint).encode()).hexdigest()[:24]
            with self.db() as con:
                same=con.execute('SELECT * FROM processes WHERE id=?',(pid,)).fetchone()
                con.execute("UPDATE processes SET active=0 WHERE source_kind='upload' AND process_key=? AND id<>?",(key,pid))
                if same:
                    status=same['status']
                    if status=='erro' or (json.loads(same['result']) if same['result'] else {}).get('failures'):status='pendente';stats['resumed']+=1
                    elif status=='pendente':stats['resumed']+=1
                    else:stats['unchanged']+=1
                    con.execute('UPDATE processes SET active=1,files=?,status=? WHERE id=?',(json.dumps(files),status,pid))
                else:
                    stats['updated' if old else 'new']+=1
                    con.execute("""INSERT INTO processes(id,name,files,fingerprint,status,active,source_kind,source_id,process_key)
                      VALUES(?,?,?,?,'pendente',1,'upload',?,?)""",(pid,name,json.dumps(files),fingerprint,batch['id'],key))
                con.execute('INSERT OR IGNORE INTO upload_processes(upload_id,process_id) VALUES(?,?)',(batch['id'],pid))
        return stats
