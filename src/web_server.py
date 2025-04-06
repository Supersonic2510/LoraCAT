import os
import threading

from meshtastic.serial_interface import SerialInterface

from mesh_socket import MeshSocket, TIMEOUT_SECONDS


# Load the epic HTML frontpage from disk
def load_html():
    path = os.path.join(os.path.dirname(__file__), "template/index_.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def handle_connection(sock: MeshSocket):
    while not sock.is_closed:
        request = sock.readData(timeout=TIMEOUT_SECONDS * 100)
        if request:
            print("[SERVER] Received request, sending HTML page")
            sock.writeData(load_html().encode(), compress=True)
        else:
            print("[SERVER] No request or connection closed.")
            break

def main():
    iface = SerialInterface(devPath="YOUR_DEVICE")
    listener = MeshSocket(iface, None, None)
    listener.bind()

    def accept_handler(sock: MeshSocket):
        threading.Thread(target=handle_connection, args=(sock,), daemon=True).start()

    listener.set_accept_handler(accept_handler)
    print("[SERVER] Ready to serve HTML page over LoRa")

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("[SERVER] Shutdown")

if __name__ == "__main__":
    main()
