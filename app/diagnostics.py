"""Safe diagnostics: classify errors without exposing passwords or raw library messages."""
import errno
import socket
from smbprotocol.header import NtStatus
from smbprotocol.exceptions import SMBAuthenticationError, SMBConnectionClosed
from spnego.exceptions import SpnegoError


class ConnectionDiagnosticError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.safe_message = message
        super().__init__(message)


def explain_error(exc):
    if isinstance(exc, ConnectionDiagnosticError):
        return exc.code, exc.safe_message
    if isinstance(exc, socket.gaierror):
        return 'dns_failure', 'Nome do servidor não resolvido pelo contêiner. Use o IP ou configure o DNS da rede interna no servidor Docker.'
    status = getattr(exc, 'ntstatus', None)
    if status is None:
        try:
            status = getattr(exc, 'status', None)
        except Exception:
            pass
    statuses = {
        'STATUS_LOGON_FAILURE': ('smb_auth_failed', 'Windows recusou a autenticação SMB. Confira usuário, senha e domínio; para conta local, use o nome do servidor como domínio.'),
        'STATUS_WRONG_PASSWORD': ('smb_auth_failed', 'Windows recusou a senha da conta SMB. Confira a senha salva.'),
        'STATUS_NO_SUCH_USER': ('smb_auth_failed', 'Windows não reconheceu a conta SMB. Confira usuário e domínio.'),
        'STATUS_ACCOUNT_DISABLED': ('smb_account_disabled', 'A conta Windows está desabilitada. Solicite a verificação ao administrador da rede.'),
        'STATUS_ACCOUNT_LOCKED_OUT': ('smb_account_locked', 'A conta Windows está bloqueada. Interrompa as tentativas e verifique a conta com o administrador.'),
        'STATUS_PASSWORD_EXPIRED': ('smb_password_expired', 'A senha da conta Windows expirou. Atualize a conta e depois a senha salva no painel.'),
        'STATUS_PASSWORD_MUST_CHANGE': ('smb_password_expired', 'A conta Windows exige troca de senha antes do acesso SMB.'),
        'STATUS_ACCESS_DENIED': ('smb_access_denied', 'Windows negou acesso. Confira as permissões de leitura/listagem do compartilhamento e as permissões NTFS da pasta para esta conta.'),
        'STATUS_PRIVILEGE_NOT_HELD': ('smb_access_denied', 'A conta Windows não possui o privilégio necessário para acessar esta pasta.'),
        'STATUS_BAD_NETWORK_NAME': ('smb_share_not_found', 'O compartilhamento SMB não foi encontrado. Informe somente seu nome, por exemplo Informatica; a pasta Arquivos/Isencao_IPTU pertence ao campo Subpasta.'),
        'STATUS_OBJECT_PATH_NOT_FOUND': ('smb_path_not_found', 'A subpasta não foi encontrada dentro do compartilhamento. Confira a grafia e o caminho relativo.'),
        'STATUS_OBJECT_NAME_NOT_FOUND': ('smb_path_not_found', 'O arquivo ou a pasta não foi encontrado no compartilhamento. Confira a subpasta e atualize o inventário.'),
        'STATUS_NOT_FOUND': ('smb_path_not_found', 'O arquivo ou a pasta não foi encontrado no compartilhamento.'),
        'STATUS_SHARING_VIOLATION': ('smb_file_in_use', 'O Windows informou que o arquivo está em uso. Aguarde a liberação e tente novamente.'),
        'STATUS_NOT_SUPPORTED': ('smb_not_supported', 'O servidor recusou o recurso SMB solicitado. Verifique suporte a SMB 2/3 e políticas de autenticação com a equipe de rede.'),
    }
    for name, info in statuses.items():
        if status is not None and status == getattr(NtStatus, name, object()):
            return info
    if isinstance(exc, (SMBAuthenticationError, SpnegoError)):
        return 'smb_auth_failed', 'Falha na autenticação/negociação da conta SMB. Confira usuário, senha, domínio e políticas de NTLM/Kerberos com a equipe de rede.'
    if isinstance(exc, SMBConnectionClosed):
        return 'smb_connection_closed', 'A conexão foi encerrada durante a negociação SMB. Confira firewall, políticas SMB 2/3 e conectividade da VPN.'
    if isinstance(exc, ConnectionRefusedError):
        return 'tcp445_refused', 'Conexão TCP 445 recusada pelo destino. Confira se o serviço SMB está ativo e se o firewall permite acesso a partir do servidor Docker.'
    if isinstance(exc, TimeoutError) or getattr(exc, 'errno', None) == errno.ETIMEDOUT:
        return 'tcp445_timeout', 'Tempo de conexão esgotado. O contêiner não recebeu resposta do Windows. Confira VPN/rota, firewall TCP 445 e possível sobreposição da subrede Docker com a rede Windows.'
    if isinstance(exc, OSError):
        number = exc.errno
        if number in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return 'network_unreachable', 'Rede/host Windows inacessível a partir do contêiner. Configure a rota ou VPN e confira se a subrede Docker se sobrepõe à rede Windows.'
        if number in (errno.EACCES, errno.EPERM):
            return 'permission_denied', 'Acesso negado. Confira permissões da conta no compartilhamento/NTFS e permissões de gravação no volume /data.'
        if number == errno.ECONNRESET:
            return 'connection_reset', 'A conexão foi reiniciada pelo destino ou firewall. Confira conectividade e políticas SMB.'
    if status is not None and isinstance(status, int):
        return 'smb_unknown_status', f'Windows retornou status SMB 0x{status:08X}. Informe este código ao administrador da rede; nenhuma credencial é exibida.'
    if isinstance(exc, ValueError):
        return 'invalid_input', str(exc)[:200]
    return 'unexpected_error', f'Falha não classificada ({type(exc).__name__}). Verifique a etapa do diagnóstico e os registros do aplicativo no Coolify.'


def probe_tcp445(server, timeout=8):
    """Runs on the application server, so it tests the actual container's network."""
    try:
        with socket.create_connection((server, 445), timeout=timeout):
            return 'Porta TCP 445 acessível a partir do contêiner.'
    except OSError as exc:
        code, message = explain_error(exc)
        raise ConnectionDiagnosticError(code, message) from None
