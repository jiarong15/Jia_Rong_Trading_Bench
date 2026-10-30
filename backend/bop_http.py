"""Verified HTTPS for public BOP sources, including optional local CA trust."""
from functools import lru_cache
import os
from pathlib import Path
import ssl
from urllib.request import HTTPSHandler
from urllib.request import urlopen as standard_urlopen

PROJECT=Path(__file__).resolve().parents[1]


@lru_cache(maxsize=4)
def _trusted_context(path,modified):
    context=ssl.create_default_context()
    try:context.load_verify_locations(cafile=path)
    except (OSError,ssl.SSLError):raise ValueError('BOP_CA_FILE could not be loaded as a trusted PEM certificate bundle.') from None
    return context


def tls_context():
    configured=os.environ.get('BOP_CA_FILE','').strip()
    if not configured:return None  # urllib's normal verified system trust
    path=Path(configured).expanduser()
    if not path.is_absolute():path=PROJECT/path
    try:
        stat=path.stat()
        if not path.is_file() or stat.st_size>1_000_000:raise OSError()
    except OSError:raise ValueError('BOP_CA_FILE must identify a readable local PEM certificate bundle (at most 1 MB).') from None
    return _trusted_context(str(path.resolve()),stat.st_mtime_ns)


def urlopen(request,*,timeout):
    context=tls_context()
    if context is None:return standard_urlopen(request,timeout=timeout)
    return standard_urlopen(request,timeout=timeout,context=context)


def https_handler():
    return HTTPSHandler(context=tls_context())
