"""TLS trust setup for outbound HTTP.

Corporate networks (this project's primary machine included) terminate TLS at
an inspecting proxy and re-sign certificates with a private CA. That CA is
installed in the Windows certificate store, but Python's ``ssl`` module uses
certifi's bundle instead and every request dies with
CERTIFICATE_VERIFY_FAILED.

``truststore`` is a pure-Python shim that points ``ssl`` at the OS trust store,
which fixes it without admin rights, without disabling verification, and
without shipping a CA bundle in the repo.
"""

from __future__ import annotations

from recsys.log import get_logger

log = get_logger(__name__)

_INJECTED = False


def enable_os_truststore() -> bool:
    """Route TLS verification through the OS certificate store. Idempotent."""
    global _INJECTED
    if _INJECTED:
        return True
    try:
        import truststore

        truststore.inject_into_ssl()
        _INJECTED = True
        log.debug("TLS verification now uses the OS trust store")
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("could not enable the OS trust store (%s); using certifi", exc)
        return False
