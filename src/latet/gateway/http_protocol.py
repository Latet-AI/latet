"""The shipped h11 profile bounds queued input before parsing the next batch."""

from h11._receivebuffer import ReceiveBuffer
from uvicorn.protocols.http.h11_impl import H11Protocol

MAX_UNREAD_HTTP_BYTES = 512 * 1024


class BoundedH11Protocol(H11Protocol):
    def data_received(self, data: bytes) -> None:
        if self.transport.is_closing():
            return
        # The pinned h11 receive buffer stores unread parser input in a bytearray.
        buffer = getattr(self.conn, "_receive_buffer", None)
        unread = getattr(buffer, "_data", None)
        if (
            type(buffer) is not ReceiveBuffer
            or type(unread) is not bytearray
            or len(unread) + len(data) > MAX_UNREAD_HTTP_BYTES
        ):
            self.transport.close()
            return
        super().data_received(data)
