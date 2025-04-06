import threading
import time

from meshtastic.serial_interface import SerialInterface

from mesh_socket import MeshSocket

# Dictionary to hold active connections
connections = {}

def handle_client(sock: MeshSocket):
    print(f"[SERVER] Handling connection {sock.connection_id} from {sock.remote_node_id}")
    while not sock.is_closed:
        data = sock.readData(timeout=10)
        if data is None:
            if sock.is_closed:
                print(f"[SERVER] Client {sock.connection_id} disconnected cleanly.")
            else:
                print(f"[SERVER] Timeout waiting for data on {sock.connection_id}")
            break
        print(f"[SERVER] Received: {data.decode()}")
        sock.writeData(b"Message received!" * 20, compress=True)

def main():
    iface = SerialInterface(devPath="YOUR_DEVICE")
    listener = MeshSocket(iface, None, None)

    # Accept new clients and launch a thread per connection
    def on_accept(sock):
        connections[sock.connection_id] = sock
        threading.Thread(target=handle_client, args=(sock,), daemon=True).start()

    listener.set_accept_handler(on_accept)
    listener.bind()

    print("[SERVER] Ready and waiting for connections...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[SERVER] Shutting down.")

if __name__ == "__main__":
    main()

