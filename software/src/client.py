import time

from meshtastic.serial_interface import SerialInterface

from mesh_socket import MeshSocket


def main():
    iface = SerialInterface(devPath="YOUR_DEVICE")
    remote_node_id = "YOUR_REMOTE_NODE_ID"

    sock = MeshSocket.connect(iface, remote_node_id)
    if not sock:
        print("[CLIENT] Connection failed.")
        return

    print(f"[CLIENT] Connected with ID {sock.connection_id}")
    sock.writeData(b"Hello from client!" * 20, compress=True)

    response = sock.readData(timeout=10)
    if response:
        print(f"[CLIENT] Server responded: {response.decode()}")
    else:
        print("[CLIENT] No response received.")

    time.sleep(1)
    sock.disconnect()
    print("[CLIENT] Disconnected.")

if __name__ == "__main__":
    main()
