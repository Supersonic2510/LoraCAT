import webview
from meshtastic.serial_interface import SerialInterface

from mesh_socket import MeshSocket, TIMEOUT_SECONDS


def main():
    iface = SerialInterface(devPath="YOUR_DEVICE")
    remote_node_id = "YOUR_REMOTE_NODE_ID"

    sock = MeshSocket.connect(iface, remote_node_id)
    if not sock:
        print("[CLIENT] Connection failed")
        return

    sock.writeData(b"GET / HTTP/1.1\n\n", compress=True)
    print("[CLIENT] Sent request")

    response = sock.readData(timeout=TIMEOUT_SECONDS * 100)
    if response:
        html = response.decode()
        print("[CLIENT] Received HTML page")
        webview.create_window("LoRa WebView", html=html)
        webview.start()
    else:
        print("[CLIENT] No response received")

    sock.disconnect()

if __name__ == "__main__":
    main()
