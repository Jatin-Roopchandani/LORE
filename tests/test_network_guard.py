import socket

import pytest


def test_remote_connect_is_blocked() -> None:
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="network blocked"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


def test_loopback_is_allowed() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.socket()
    try:
        c.connect(srv.getsockname())
    finally:
        c.close()
        srv.close()
