"""Suite-wide guard: no test may reach a non-local host.

Anything that needs the real network says so with @pytest.mark.network.
Loopback is allowed (Ollama and test servers bind there).
"""
import socket

import pytest

_LOCAL = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(address) -> bool:
    if isinstance(address, (str, bytes)):  # a unix socket path
        return True
    return str(address[0]) in _LOCAL


@pytest.fixture(autouse=True)
def _block_remote_sockets(request, monkeypatch):
    if request.node.get_closest_marker("network"):
        return

    def guarded(self, address):
        if not _is_local(address):
            raise RuntimeError(f"network blocked in tests: connect to {address!r}")
        return _real_connect(self, address)

    def guarded_ex(self, address):
        if not _is_local(address):
            raise RuntimeError(f"network blocked in tests: connect to {address!r}")
        return _real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_ex)
