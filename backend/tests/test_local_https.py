from cryptography import x509
from fastapi.testclient import TestClient

from app.core.localhost_tls import ensure_localhost_certificate
from app.local_https import callback_app


def test_instagram_callback_preserves_query() -> None:
    client = TestClient(callback_app, follow_redirects=False)
    response = client.get("/api/oauth/instagram/callback?code=abc&state=xyz")
    assert response.status_code == 302
    assert response.headers["location"] == (
        "http://127.0.0.1:8765/api/oauth/instagram/callback?code=abc&state=xyz"
    )


def test_localhost_certificate_is_leaf_and_has_expected_names(tmp_path) -> None:
    cert_path = tmp_path / "localhost.crt.pem"
    key_path = tmp_path / "localhost.key.pem"
    ensure_localhost_certificate(cert_path, key_path)

    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert cert_path.is_file()
    assert key_path.is_file()
