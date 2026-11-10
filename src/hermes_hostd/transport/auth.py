"""Requester bearer credentials, separate from operations and operator approval."""
import hmac
import logging
import os
import re
import stat

MAX_TOKEN_BYTES = 256
_TOKEN = re.compile(r'[A-Za-z0-9._~+/-]{1,256}={0,2}\Z', re.ASCII)


def validate_token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TOKEN_BYTES or not _TOKEN.fullmatch(value):
        raise ValueError('invalid requester token (expected 1..256 ASCII bearer characters)')
    return value


def read_token_file(path):
    """Read a bounded private regular file, without following a leaf symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise ValueError('unsafe file')
            raw = stream.read(MAX_TOKEN_BYTES + 2)
        if raw.endswith(b'\n'):
            raw = raw[:-1]
        return validate_token(raw.decode('ascii'))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError('cannot read requester token from private owned regular file') from None


def token_from_config(config):
    """Explicit file > named environment (default HOSTD_AUTH_TOKEN); no fallback on failure."""
    if 'auth_token_file' in config:
        if 'auth_token' in config:
            raise ValueError('choose one requester token source')
        return read_token_file(config['auth_token_file'])
    if 'auth_token' in config:  # compatibility; prefer file/env in operator config
        return config['auth_token']
    name = config.get('auth_token_env', 'HOSTD_AUTH_TOKEN')
    if not isinstance(name, str) or not name:
        raise ValueError('invalid requester token environment setting')
    return os.environ.get(name)


class RequesterAuth:
    def __init__(self, token=None, insecure_no_auth=False):
        if type(insecure_no_auth) is not bool:
            raise ValueError('insecure_no_auth must be an explicit boolean')
        if insecure_no_auth:
            if token is not None:
                raise ValueError('requester token and insecure_no_auth are mutually exclusive')
            logging.getLogger(__name__).warning(
                'INSECURE: requester authentication explicitly disabled; any reachable caller can submit approvals')
            self._token = None
        else:
            if token is None:
                raise ValueError('requester token required; configure a private token source or explicitly set insecure_no_auth: true')
            self._token = validate_token(token).encode('ascii')

    def accepts(self, token):
        if self._token is None:
            return token is None  # legacy requests only; never ignore supplied auth
        try:
            value = validate_token(token).encode('ascii')
        except ValueError:
            return False
        return hmac.compare_digest(self._token, value)

    def accepts_header(self, value):
        if value is None:
            return self.accepts(None)
        if not isinstance(value, bytes) or len(value) > MAX_TOKEN_BYTES + 7 or not value.startswith(b'Bearer '):
            return False
        try:
            return self.accepts(value[7:].decode('ascii'))
        except UnicodeError:
            return False
