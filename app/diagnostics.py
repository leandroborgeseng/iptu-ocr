"""Safe diagnostics for local upload, storage and OCR operations only."""
import errno
import subprocess
import pymupdf as fitz


class ProcessingDiagnosticError(Exception):
    def __init__(self,code,message):self.code=code;self.safe_message=message;super().__init__(message)


def explain_error(exc):
    if isinstance(exc,ProcessingDiagnosticError):return exc.code,exc.safe_message
    if isinstance(exc,subprocess.TimeoutExpired):return 'ocr_timeout','O OCR excedeu o tempo limite desta página. O trabalho concluído foi preservado.'
    if isinstance(exc,fitz.FileDataError):return 'invalid_pdf','Não foi possível ler este PDF. Confira se ele está íntegro e não está protegido por senha.'
    if isinstance(exc,OSError):
        if exc.errno==errno.ENOSPC:return 'storage_full','O volume de dados ficou sem espaço. Libere espaço ou amplie o volume antes de retomar.'
        if exc.errno in (errno.EACCES,errno.EPERM):return 'storage_permission','Sem permissão para ler ou gravar no volume /data. Confira as permissões do contêiner.'
        if exc.errno==errno.ENOENT:return 'file_missing','Um arquivo local não está disponível. Reenvie a seleção para conferir e restaurar o que falta.'
    if isinstance(exc,ValueError):return 'invalid_input',str(exc)[:300]
    return 'unexpected_error',f'Falha não classificada ({type(exc).__name__}). Confira os registros do aplicativo no Coolify.'
