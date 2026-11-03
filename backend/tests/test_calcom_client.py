import asyncio
import ipaddress
import json
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.calcom.client import CalClient, CalError


@pytest.mark.asyncio
async def test_custom_ca_trust_with_real_tls_and_no_ambient_proxy(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local test CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    requests = []
    body = json.dumps({"status": "success", "data": {"id": 42, "username": "owner"}}).encode()

    async def respond(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    async with await asyncio.start_server(respond, "127.0.0.1", 0, ssl=tls) as server:
        url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        client = CalClient("offline-key")
        client.http.base_url = url
        try:
            with pytest.raises(CalError, match="trusted TLS CA certificates") as error:
                await client.request("GET", "/v2/me")
            assert not error.value.uncertain
            assert not requests
        finally:
            await client.close()

        monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
        client = CalClient("offline-key")
        client.http.base_url = url
        try:
            assert await client.request("GET", "/v2/me") == {"id": 42, "username": "owner"}
            assert len(requests) == 1
            assert b"GET /v2/me HTTP/1.1" in requests[0]
            assert b"Authorization: Bearer offline-key" in requests[0]
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError])
async def test_transport_errors_are_sanitized_and_only_writes_are_uncertain(method, error_type):
    async def fail(request):
        raise error_type("private key, URL and certificate details", request=request)

    client = CalClient("offline-key", httpx.MockTransport(fail))
    try:
        with pytest.raises(CalError) as error:
            await client.request(method, "/v2/me")
        assert "private" not in str(error.value)
        assert "offline-key" not in str(error.value)
        assert error.value.uncertain == (method == "POST")
        assert ("outcome may be unknown" in str(error.value)) == (method == "POST")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_key_is_distinct_from_transport_failure():
    client = CalClient(
        "offline-key",
        httpx.MockTransport(lambda request: httpx.Response(401, json={"private": "details"})),
    )
    try:
        with pytest.raises(CalError, match="key expired or invalid") as error:
            await client.request("GET", "/v2/me")
        assert not error.value.uncertain
    finally:
        await client.close()
