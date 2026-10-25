"""A tiny Modbus TCP client: just enough to read input registers (function code 4).

Only *reads* are implemented on purpose, so this module cannot change inverter settings.

Frame layout (all big-endian), request::

    transaction id (2) | protocol id = 0 (2) | length = 6 (2) | unit id (1) | function = 4 (1)
    | first register (2) | register count (2)

Response::

    transaction id (2) | protocol id (2) | length (2) | unit id (1) | function (1)
    | byte count (1) | register values (2 bytes each)

A function code with the high bit set (0x84) is an exception response; its single data byte is the
Modbus exception code.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Callable
from typing import Protocol

READ_INPUT_REGISTERS = 4


class ModbusError(IOError):
    """The device answered with a Modbus exception, or the connection broke mid-frame."""


class _Socket(Protocol):
    """The subset of :class:`socket.socket` this client uses (lets tests pass a fake)."""

    def sendall(self, data: bytes, /) -> None: ...  # noqa: D102
    def recv(self, size: int, /) -> bytes: ...  # noqa: D102
    def close(self) -> None: ...  # noqa: D102


Connector = Callable[[tuple[str, int], float], _Socket]


def _connect(address: tuple[str, int], timeout: float) -> _Socket:
    """Default connector: a real TCP connection."""
    return socket.create_connection(address, timeout=timeout)


def build_request(unit: int, address: int, count: int, transaction: int = 1) -> bytes:
    """Return the request frame for reading ``count`` input registers starting at ``address``."""
    if not 1 <= count <= 125:
        raise ValueError("Modbus allows reading 1-125 registers per request")
    return struct.pack(">HHHBBHH", transaction, 0, 6, unit, READ_INPUT_REGISTERS, address, count)


def _recv_exact(sock: _Socket, size: int) -> bytes:
    """Read exactly ``size`` bytes or raise :class:`ModbusError` if the peer closes early."""
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ModbusError("connection closed before the response was complete")
        data += chunk
    return data


def parse_response(header: bytes, payload: bytes, address: int) -> dict[int, int]:
    """Decode a response into ``{register address: value}``.

    Args:
        header: The first 9 bytes of the response (MBAP header, function code, byte count).
        payload: The register bytes that followed.
        address: The first register that was requested, used to number the values.

    Raises:
        ModbusError: For exception responses.
    """
    function, byte_count = header[7], header[8]
    if function & 0x80:
        raise ModbusError(f"Modbus exception {byte_count} reading register {address}")
    values = struct.unpack(f">{byte_count // 2}H", payload[:byte_count])
    return {address + i: v for i, v in enumerate(values)}


def read_input_registers(
    host: str,
    address: int,
    count: int,
    *,
    port: int = 502,
    unit: int = 1,
    timeout: float = 10.0,
    connect: Connector = _connect,
) -> dict[int, int]:
    """Read a block of input registers from a Modbus TCP device.

    Returns:
        Mapping from register address to its unsigned 16-bit value.

    Raises:
        OSError: If the device is unreachable (including :class:`ModbusError`).
    """
    sock = connect((host, port), timeout)
    try:
        sock.sendall(build_request(unit, address, count))
        header = _recv_exact(sock, 9)
        if header[7] & 0x80:
            return parse_response(header, b"", address)
        return parse_response(header, _recv_exact(sock, header[8]), address)
    finally:
        sock.close()
