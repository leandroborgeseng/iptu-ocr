"""Read-only SMB source; no kernel mount or privileged container required."""
import ntpath
import time
from pathlib import PureWindowsPath
import smbclient


def validate(config):
    server = str(config.get('server', '')).strip()
    share = str(config.get('share', '')).strip()
    sub = str(config.get('subpath', '')).strip().replace('/', '\\').strip('\\')
    if not server or any(c in server for c in '/\\:@\n\r '):
        raise ValueError('Informe apenas o IP ou nome do servidor, sem \\\\ ou smb://.')
    if not share or any(c in share for c in '/\\\n\r') or share in ('.', '..'):
        raise ValueError('Informe o nome do compartilhamento, sem barras.')
    if '..' in PureWindowsPath(sub).parts or ':' in sub:
        raise ValueError('A subpasta deve ser relativa ao compartilhamento.')
    if not str(config.get('username', '')).strip():
        raise ValueError('Informe o usuário do Windows.')
    config.update(server=server, share=share, subpath=sub)
    config['stable_seconds'] = max(30, min(86400, int(config.get('stable_seconds', 120))))
    return config


class SMBSource:
    def __init__(self, config):
        self.config = validate(dict(config))
        user = self.config['username']
        domain = self.config.get('domain', '').strip()
        if domain and '\\' not in user and '@' not in user:
            user = domain + '\\' + user
        smbclient.reset_connection_cache()
        smbclient.register_session(self.config['server'], username=user,
                                   password=self.config.get('password', ''),
                                   connection_timeout=20)
        self.root = '\\\\' + self.config['server'] + '\\' + self.config['share']
        if self.config['subpath']:
            self.root = ntpath.join(self.root, self.config['subpath'])

    def test(self):
        with smbclient.scandir(self.root) as entries:
            return [entry.name for _, entry in zip(range(10), entries)]

    def inventory(self):
        result = {}
        def fail(error):
            raise error
        for directory, _, names in smbclient.walk(self.root, onerror=fail, follow_symlinks=False):
            for name in names:
                if not name.lower().endswith('.pdf'):
                    continue
                path = ntpath.join(directory, name)
                relative = ntpath.relpath(path, self.root)
                parts = PureWindowsPath(relative).parts
                process = parts[0] if len(parts) > 1 else '_raiz'
                stat = smbclient.stat(path)
                result.setdefault(process, []).append(dict(path=path, relative=relative,
                    size=stat.st_size, mtime=stat.st_mtime))
        return result

    def copy(self, item, destination):
        before = smbclient.stat(item['path'])
        if (before.st_size != item['size'] or before.st_mtime != item['mtime'] or
                time.time() - before.st_mtime < self.config['stable_seconds']):
            raise ValueError('Arquivo alterado ou ainda recente. Atualize o inventário e tente novamente.')
        with smbclient.open_file(item['path'], mode='rb') as source, open(destination, 'wb') as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        after = smbclient.stat(item['path'])
        if (after.st_size, after.st_mtime) != (before.st_size, before.st_mtime):
            raise ValueError('Arquivo mudou durante a cópia. Atualize o inventário.')
        if destination.stat().st_size != before.st_size:
            raise ValueError('Cópia incompleta do PDF.')
