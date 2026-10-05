# -*- coding: utf-8 -*-
"""线格式（wire）单测：边界、坏报文、超时、断线。"""

from __future__ import annotations

import socket

import pytest

from wire import WireConnection, WireError


def _pair():
    a, b = socket.socketpair()
    return WireConnection(a), WireConnection(b)


def test_roundtrip_preserves_utf8_and_batch():
    ca, cb = _pair()
    try:
        ca.send({"type": "hello", "note": "中文 ✓"})
        ca.send({"type": "ping"})
        assert cb.recv(timeout=1)["note"] == "中文 ✓"
        assert cb.recv(timeout=1)["type"] == "ping"
    finally:
        ca.close()
        cb.close()


def test_bad_json_is_wire_error():
    ca, cb = _pair()
    try:
        ca.sock.sendall(b"not json\n")
        with pytest.raises(WireError) as ei:
            cb.recv(timeout=1)
        assert ei.value.code == "bad_json"
    finally:
        ca.close()
        cb.close()


def test_message_without_type_rejected():
    ca, cb = _pair()
    try:
        ca.sock.sendall(b'{"a":1}\n')
        with pytest.raises(WireError) as ei:
            cb.recv(timeout=1)
        assert ei.value.code == "bad_message"
    finally:
        ca.close()
        cb.close()


def test_timeout_is_wire_error():
    ca, cb = _pair()
    try:
        with pytest.raises(WireError) as ei:
            cb.recv(timeout=0.1)
        assert ei.value.code == "timeout"
    finally:
        ca.close()
        cb.close()


def test_peer_close_is_wire_error():
    ca, cb = _pair()
    try:
        ca.close()
        with pytest.raises(WireError) as ei:
            cb.recv(timeout=1)
        assert ei.value.code in ("closed", "io")
    finally:
        cb.close()
