import threading
import json
import base64
import zlib
import time
from enum import StrEnum
from typing import Optional, Any, Callable
from pubsub import pub
from meshtastic.protobuf.portnums_pb2 import TEXT_MESSAGE_APP

# Constants for data transfer limits
MAX_BYTES = 220
MAX_CHUNKS = 999
MAX_RETRIES = 10

# Private apps values >= 256
WEBSOCKET_SOCKET_PORT_APP = 433

# Timeout Value -> Based on bytes and ToA
TIMEOUT_SECONDS = 60

# Communication flags to describe message types and transport structure
class SocketFlag(StrEnum):
    CONN_REQUEST = "CONN_REQUEST"   # Client requests to connect
    CONN_ACCEPT = "CONN_ACCEPT"     # Server accepts connection
    CONN_DENY = "CONN_DENY"         # Server denies connection
    CONN_CLOSE = "CONN_CLOSE"       # One side is closing connection
    DATA = "DATA"                   # Payload contains application data
    PING = "PING"                   # Optional heartbeat
    PONG = "PONG"                   # Response to PING
    ERROR = "ERROR"                 # Error communication
    CHUNK = "CHUNK"                 # Payload is a chunked part
    CHUNK_END = "CHUNK_END"         # Final chunk of multi-part payload
    ACK = "ACK"                     # Acknowledgment

# Represents a structured message payload (JSON encoding)
class MeshPayload(json.JSONEncoder):
    def __init__(
        self,
        flag: Optional[set[SocketFlag]] = None,
        data: Optional[bytes] = None,
        connection_id: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None
    ):
        super().__init__()
        self.flag = flag or set()
        self.data = data
        self.connection_id = connection_id
        self.meta = meta or {}

    def to_json(self) -> str:
        return json.dumps({
            "flag": [f.name for f in self.flag],
            "data": base64.b64encode(self.data).decode("utf-8") if self.data else None,
            "connection_id": self.connection_id,
            "meta": self.meta
        })

    @classmethod
    def from_json(cls, json_str: str) -> "MeshPayload":
        obj = json.loads(json_str)
        flag_set = {SocketFlag(flag_name) for flag_name in obj.get("flag", [])}
        raw_data = obj.get("data")
        data = base64.b64decode(raw_data.encode("utf-8")) if raw_data else None
        return cls(
            flag=flag_set,
            data=data,
            connection_id=obj.get("connection_id"),
            meta=obj.get("meta", {})
        )

# Core socket abstraction over Meshtastic
class MeshSocket:
    def __init__(self, iface, remote_node_id: Optional[str], connection_id: Optional[str]):
        self.iface = iface
        self.remote_node_id = remote_node_id
        self.connection_id = connection_id
        self.read_event = threading.Event()
        self.read_data = None
        self.expected_chunks = None
        self.received_chunks = {}
        self._on_accept: Optional[Callable[[Any], None]] = None
        self.closed = False

    @property
    def is_closed(self) -> bool:
        """Public accessor to check if the connection has been closed."""
        return self.closed

    # Internal write logic: compression + chunking + transport
    def _write(self, payload: MeshPayload, compress: bool = False):
        data = payload.data
        if compress and data:
            data = zlib.compress(data)
            print("[INFO] Compressed payload with zlib")
        payload.data = data
        payload.meta["encoding"] = "base64"
        if compress:
            payload.meta["compression"] = "zlib"

        json_bytes = payload.to_json().encode("utf-8")
        if len(json_bytes) <= MAX_BYTES:
            self._send(json_bytes, "single packet")
            return

        print("[CHUNKING] Message too large — chunking")
        # Create a dummy payload with only metadata (to calculate overhead)
        dummy_payload = MeshPayload(
            flag=set(payload.flag) | {SocketFlag.CHUNK} | {SocketFlag.CHUNK_END},
            data=None,
            connection_id=self.connection_id,
            meta={**payload.meta, "chunk_index": MAX_CHUNKS, "total_chunks": MAX_CHUNKS}
        )
        dummy_json_bytes = dummy_payload.to_json().encode("utf-8")
        dummy_size = len(dummy_json_bytes)

        chunk_size = MAX_BYTES - dummy_size
        total_chunks = (len(data) + chunk_size - 1) // chunk_size
        print(f"[DEBUG] Dummy size: {dummy_size}, chunk_size: {chunk_size}, total_chunks: {total_chunks}")

        for i in range(total_chunks):
            start = i * chunk_size
            end = start + chunk_size
            chunk_data = data[start:end]
            flags = set(payload.flag) | {SocketFlag.CHUNK}
            if i == total_chunks - 1:
                flags.add(SocketFlag.CHUNK_END)

            chunk_payload = MeshPayload(
                flag=flags,
                data=chunk_data,
                connection_id=self.connection_id,
                meta={**payload.meta, "chunk_index": i + 1, "total_chunks": total_chunks}
            )
            retries = 0
            while retries < MAX_RETRIES:
                # Send the chunk
                chunk_json = chunk_payload.to_json()
                self._send(chunk_json.encode("utf-8"), f"chunk {i + 1}/{total_chunks}")
                print(f"[SEND CHUNK] Sent chunk {i + 1}/{total_chunks}, attempt {retries + 1}")

                # Create an Event to wait for the ACK
                ack_received = threading.Event()

                def _on_ack(packet, interface=None):
                    try:

                        if packet["decoded"].get("portnum") != WEBSOCKET_SOCKET_PORT_APP:
                            return  # Ignore other app types

                        raw = packet["decoded"].get("payload")
                        print(f"[DEBUG] _on_ack raw payload: {raw!r}")
                        if not raw:
                            print("[DEBUG] Received empty ACK payload")
                            return
                        # Attempt to decode as UTF-8 JSON
                        ack_str = raw.decode("utf-8")
                        print(f"[DEBUG] _on_ack decoded string: {ack_str}")
                        ack_payload = MeshPayload.from_json(ack_str)
                        if (SocketFlag.ACK in ack_payload.flag and
                                ack_payload.meta.get("chunk_index") == i + 1 and
                                ack_payload.connection_id == self.connection_id):
                            print(f"[DEBUG] ACK received for chunk {i + 1}")
                            ack_received.set()
                    except Exception as e:
                        print(f"[ERROR] _on_ack exception: {e}")
                        pass

                pub.subscribe(_on_ack, "meshtastic.receive")
                print(f"[WAIT] Waiting for ACK for chunk {i + 1}/{total_chunks}, attempt {retries + 1}")
                if ack_received.wait(timeout=TIMEOUT_SECONDS):
                    pub.unsubscribe(_on_ack, "meshtastic.receive")
                    print(f"[ACK RECEIVED] Chunk {i + 1}/{total_chunks} acknowledged on attempt {retries + 1}")
                    break
                pub.unsubscribe(_on_ack, "meshtastic.receive")
                retries += 1
                print(f"[RETRY] No ACK for chunk {i + 1}, retry {retries}")
            if retries >= MAX_RETRIES:
                print(f"[ERROR] Failed to receive ACK after {MAX_RETRIES} retries for chunk {i + 1}/{total_chunks}")

    # Wrapper for sending raw data to a node
    def _send(self, data: bytes, label: str = ""):
        self.iface.sendData(
            data=data,
            destinationId=self.remote_node_id,
            wantAck=True,
            wantResponse=False,
            portNum=WEBSOCKET_SOCKET_PORT_APP
        )
        print(f"[SEND] {label} to {self.remote_node_id} on connection {self.connection_id}")

    def _send_ack(self, chunk_index: int):
        ack_payload = MeshPayload(
            flag={SocketFlag.ACK},
            connection_id=self.connection_id,
            meta={"chunk_index": chunk_index}
        )
        ack_json = ack_payload.to_json()
        print(f"[DEBUG] Sending ACK payload: {ack_json}")
        self._send(ack_json.encode("utf-8"), f"ACK {chunk_index}")
        print(f"[SENT ACK] ACK for chunk {chunk_index} sent on connection {self.connection_id}")

    # Internal read: reassembly of chunks and decompression
    def _read(self, payload: MeshPayload):
        if payload.connection_id != self.connection_id:
            print(f"[SKIP] Payload for different connection ID: {payload.connection_id}")
            return

        if SocketFlag.CHUNK in payload.flag and "chunk_index" in payload.meta:
            chunk_index = payload.meta["chunk_index"]
            total_chunks = payload.meta["total_chunks"]
            if self.expected_chunks is None:
                self.expected_chunks = total_chunks
                self.received_chunks = {}

            self.received_chunks[chunk_index] = payload.data
            print(f"[RECEIVED CHUNK] Received chunk {chunk_index}/{total_chunks} on connection {self.connection_id}")
            print(f"[PROCESS CHUNK] Processing received chunk {chunk_index} and sending ACK")
            # Send ACK for this chunk immediately after receiving it
            self._send_ack(chunk_index)
            print(f"[ACK SENT] ACK for chunk {chunk_index} has been sent")

            if len(self.received_chunks) == self.expected_chunks:
                sorted_chunks = [self.received_chunks[i] for i in range(1, total_chunks + 1)]
                full_data = b"".join(sorted_chunks)
                if payload.meta.get("compression") == "zlib":
                    try:
                        full_data = zlib.decompress(full_data)
                        print("[INFO] Decompressed reassembled data")
                    except Exception as e:
                        print("[ERROR] Decompression failed:", e)
                self.read_data = full_data
                self.read_event.set()
                self.expected_chunks = None
                self.received_chunks = {}
        else:
            data = payload.data
            if payload.meta.get("compression") == "zlib":
                try:
                    data = zlib.decompress(data)
                    print("[INFO] Decompressed single-packet data")
                except Exception as e:
                    print("[ERROR] Decompression failed:", e)
            self.read_data = data
            self.read_event.set()

    def writeData(self, data: bytes, compress: bool = False):
        payload = MeshPayload(
            flag={SocketFlag.DATA},
            data=data,
            connection_id=self.connection_id
        )
        self._write(payload, compress=compress)

    def readData(self, timeout=TIMEOUT_SECONDS) -> Optional[bytes]:
        if self.closed:
            print(f"[CLOSE] Connection {self.connection_id} is closed.")
            return None
        if self.read_event.wait(timeout):
            self.read_event.clear()
            return self.read_data
        print(f"[TIMEOUT] No data received on connection {self.connection_id}")
        return None

    def disconnect(self):
        payload = MeshPayload(
            flag={SocketFlag.CONN_CLOSE},
            connection_id=self.connection_id
        )
        self._write(payload)
        print(f"[DISCONNECT] Sent CONN_CLOSE to {self.remote_node_id}")

    @staticmethod
    def connect(iface, remote_node_id: str, timeout: int = 10) -> Optional["MeshSocket"]:
        connection_event = threading.Event()
        result_socket: Optional[MeshSocket] = None

        def on_receive(packet, interface=None):
            nonlocal result_socket
            try:
                if packet["decoded"].get("portnum") != WEBSOCKET_SOCKET_PORT_APP:
                    return  # Ignore other app types
                msg = packet["decoded"]["payload"].decode("utf-8")
                payload = MeshPayload.from_json(msg)
                if SocketFlag.CONN_ACCEPT in payload.flag:
                    result_socket = MeshSocket(iface, remote_node_id, payload.connection_id)
                    pub.subscribe(result_socket._on_receive, "meshtastic.receive")
                    connection_event.set()
                    print(f"[DEBUG] Received CONN_ACCEPT: {msg}")
            except Exception as e:
                print(f"[ERROR] Exception in on_receive: {e}")
                return

        pub.subscribe(on_receive, "meshtastic.receive")

        request_payload = MeshPayload(
            flag={SocketFlag.CONN_REQUEST},
            connection_id=None
        )
        iface.sendData(
            data=request_payload.to_json().encode("utf-8"),
            destinationId=remote_node_id,
            wantAck=True,
            wantResponse=False,
            portNum=WEBSOCKET_SOCKET_PORT_APP
        )
        print(f"[CONNECT] Sent CONN_REQUEST to {remote_node_id}")

        if connection_event.wait(timeout):
            pub.unsubscribe(on_receive, "meshtastic.receive")
            print(f"[CONNECTED] to {remote_node_id}")
            return result_socket
        else:
            pub.unsubscribe(on_receive, "meshtastic.receive")
            print(f"[TIMEOUT] No CONN_ACCEPT received from {remote_node_id}")
            return None

    def bind(self):
        pub.subscribe(self._on_receive, "meshtastic.receive")
        print("[BIND] Listening for connections")

    def set_accept_handler(self, handler: Callable[[Any], None]):
        self._on_accept = handler

    def _on_receive(self, packet, interface=None):
        try:
            if packet["decoded"].get("portnum") != WEBSOCKET_SOCKET_PORT_APP:
                return  # Ignore other app types
            msg = packet["decoded"]["payload"].decode("utf-8")
            payload = MeshPayload.from_json(msg)
        except Exception as e:
            print("[ERROR] Failed to decode packet:", e)
            return

        if SocketFlag.CONN_REQUEST in payload.flag:
            client_id = packet["fromId"]
            # Use the server's node ID for the connection ID.
            # Assuming the interface object has a 'myNodeId' attribute;
            # otherwise, replace with the correct server identifier.
            server_id = self.iface.myNodeId if hasattr(self.iface, "myNodeId") else "SERVER_ID"
            # The MeshSocket on the server is now created with:
            # - remote_node_id: the client's ID (so replies go to the client)
            # - connection_id: the server's own node ID
            sock = MeshSocket(self.iface, client_id, server_id)
            pub.subscribe(sock._on_receive, "meshtastic.receive")
            accept_payload = MeshPayload(
                flag={SocketFlag.CONN_ACCEPT},
                connection_id=server_id
            )
            sock._write(accept_payload)
            print(f"[ACCEPTED] Connection from {client_id} with connection id {server_id}")
            if self._on_accept:
                threading.Thread(target=self._on_accept, args=(sock,), daemon=True).start()
        elif SocketFlag.CONN_CLOSE in payload.flag:
            if payload.connection_id != self.connection_id:
                return
            self.closed = True
            self.read_event.set()
            self.read_data = None
            self.expected_chunks = None
            self.received_chunks = {}
            print(f"[CLOSE] Peer closed connection {payload.connection_id}")
            return
        elif payload.connection_id == self.connection_id:
            self._read(payload)

