"""Build semantic-service TLS contexts from explicit settings, without process globals."""

import ssl

from dataagent.settings import SemanticTLSSettings


def build_ssl_context(settings: SemanticTLSSettings) -> bool | ssl.SSLContext:
    """Preserve legacy verification behavior while supporting explicit CA and mTLS material."""
    if not settings.verify_server and settings.client_cert is None and not settings.ciphers:
        return False
    context = ssl.create_default_context(
        cafile=settings.ca_file if settings.verify_server else None
    )
    if not settings.verify_server:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if settings.client_cert is not None:
        context.load_cert_chain(certfile=settings.client_cert, keyfile=settings.client_key)
    if settings.ciphers:
        context.set_ciphers(settings.ciphers)
    return context
