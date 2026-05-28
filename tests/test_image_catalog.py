"""Tests for image catalog URL validation."""
from __future__ import annotations

import pytest

from boxer.ipc import IPCError, ERR_INVALID_PARAMS
from boxerd.image_catalog import _validate_url, _is_private_ip


def test_private_ip_detection() -> None:
    assert _is_private_ip("10.0.0.1") is True
    assert _is_private_ip("172.16.5.1") is True
    assert _is_private_ip("192.168.1.1") is True
    assert _is_private_ip("127.0.0.1") is True
    assert _is_private_ip("::1") is True
    assert _is_private_ip("8.8.8.8") is False
    assert _is_private_ip("1.1.1.1") is False


def test_http_url_rejected() -> None:
    with pytest.raises(IPCError) as exc_info:
        _validate_url("http://example.com/image.qcow2")
    assert exc_info.value.code == ERR_INVALID_PARAMS
    assert "https" in exc_info.value.message


def test_no_scheme_rejected() -> None:
    with pytest.raises(IPCError):
        _validate_url("ftp://example.com/image.qcow2")


def test_valid_https_public_url_passes(monkeypatch) -> None:
    import socket

    def mock_getaddrinfo(host, port, *args, **kwargs):
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    _validate_url("https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img")


def test_private_ip_url_rejected(monkeypatch) -> None:
    import socket

    def mock_getaddrinfo(host, port, *args, **kwargs):
        return [(None, None, None, None, ("192.168.1.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(IPCError) as exc_info:
        _validate_url("https://internal.corp/image.qcow2")
    assert "private" in exc_info.value.message.lower()
