import errno
import socket
from unittest.mock import patch, MagicMock
import pytest
from smbprotocol.header import NtStatus
from smbprotocol.exceptions import SMBOSError,SMBAuthenticationError
from app.diagnostics import explain_error,probe_tcp445,ConnectionDiagnosticError


@pytest.mark.parametrize('exception,code',[
 (socket.gaierror(-2,'secret-in-raw-error'),'dns_failure'),
 (ConnectionRefusedError('secret-in-raw-error'),'tcp445_refused'),
 (TimeoutError('secret-in-raw-error'),'tcp445_timeout'),
 (OSError(errno.EHOSTUNREACH,'secret-in-raw-error'),'network_unreachable'),
 (SMBAuthenticationError('secret-in-raw-error'),'smb_auth_failed'),
 (SMBOSError(NtStatus.STATUS_ACCESS_DENIED,r'\\server\secret-in-raw-error'),'smb_access_denied'),
 (SMBOSError(NtStatus.STATUS_BAD_NETWORK_NAME,r'\\server\secret-in-raw-error'),'smb_share_not_found'),
 (SMBOSError(NtStatus.STATUS_OBJECT_PATH_NOT_FOUND,r'\\server\secret-in-raw-error'),'smb_path_not_found'),
])
def test_classified_errors_do_not_expose_raw_secrets(exception,code):
    actual,message=explain_error(exception)
    assert actual==code
    assert 'secret-in-raw-error' not in message


def test_probe_reports_actual_container_tcp_success_and_closes_connection():
    connection=MagicMock()
    with patch('app.diagnostics.socket.create_connection',return_value=connection) as dial:
        assert '445' in probe_tcp445('windows',timeout=5)
        dial.assert_called_once_with(('windows',445),timeout=5)
        assert connection.__exit__.called


def test_probe_failure_preserves_safe_diagnostic_code():
    with patch('app.diagnostics.socket.create_connection',side_effect=TimeoutError('secret')):
        with pytest.raises(ConnectionDiagnosticError) as err:probe_tcp445('windows')
    assert err.value.code=='tcp445_timeout' and 'secret' not in str(err.value)
