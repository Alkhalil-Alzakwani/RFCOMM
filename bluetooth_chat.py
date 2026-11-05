"""Terminal Bluetooth chat using RFCOMM, with an in-memory demo transport."""

from __future__ import annotations

import argparse
import json
import queue
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

SERVICE_NAME = "CWI Bluetooth Chat"
SERVICE_UUID = "7e4c2b8a-6d3f-4f2a-9c1b-5a8d7e6f0c21"
MAX_FRAME_SIZE = 64 * 1024


class BluetoothSocket(Protocol):
    def recv(self, size: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class DiscoveredDevice:
    address: str
    name: str


class BluetoothManager:
    """RFCOMM operations isolated behind a small transport interface."""

    def __init__(self) -> None:
        self._bluetooth = None
        self._server_socket: Optional[BluetoothSocket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._on_connection: Optional[Callable[[str, str, BluetoothSocket], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None

    def _library(self):
        if self._bluetooth is None:
            try:
                import bluetooth
            except ImportError as exc:
                raise RuntimeError(
                    "Bluetooth mode requires PyBluez. Install the platform-compatible "
                    "PyBluez package and enable Bluetooth on this computer; otherwise "
                    "run with --transport mock."
                ) from exc
            self._bluetooth = bluetooth
        return self._bluetooth

    def discover_devices(self) -> list[DiscoveredDevice]:
        bluetooth = self._library()
        try:
            found = bluetooth.discover_devices(lookup_names=True, duration=8)
        except bluetooth.BluetoothError as exc:
            raise OSError(f"Bluetooth discovery failed: {exc}") from exc
        return [DiscoveredDevice(address=address, name=name or address) for address, name in found]

    def start_server(
        self,
        port: int,
        on_connection: Callable[[str, str, BluetoothSocket], None],
        on_error: Callable[[str], None],
    ) -> None:
        bluetooth = self._library()
        self._on_connection = on_connection
        self._on_error = on_error
        self._stopping.clear()
        server = None
        try:
            server = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            server.bind(("", port))
            server.listen(10)
            server.settimeout(1.0)
            bluetooth.advertise_service(
                server,
                SERVICE_NAME,
                service_id=SERVICE_UUID,
                service_classes=[SERVICE_UUID, bluetooth.SERIAL_PORT_CLASS],
                profiles=[bluetooth.SERIAL_PORT_PROFILE],
            )
        except bluetooth.BluetoothError as exc:
            if server is not None:
                server.close()
            raise OSError(f"Could not start Bluetooth listener: {exc}") from exc
        except OSError:
            if server is not None:
                server.close()
            raise

        assert server is not None
        self._server_socket = server
        self._server_thread = threading.Thread(
            target=self._accept_connections, name="bluetooth-accept", daemon=True
        )
        self._server_thread.start()

    def _accept_connections(self) -> None:
        assert self._server_socket is not None
        while not self._stopping.is_set():
            try:
                client, (address, _port) = self._server_socket.accept()
            except socket.timeout:
                continue
            except self._bluetooth.BluetoothError as exc:
                if not self._stopping.is_set() and self._on_error:
                    self._on_error(f"Bluetooth accept failed: {exc}")
                return
            except OSError as exc:
                if not self._stopping.is_set() and self._on_error:
                    self._on_error(f"Bluetooth listener stopped: {exc}")
                return
            if self._on_connection:
                self._on_connection(address, address, client)
            else:
                client.close()

    def connect_client(self, address: str) -> BluetoothSocket:
        bluetooth = self._library()
        try:
            services = bluetooth.find_service(uuid=SERVICE_UUID, address=address)
        except bluetooth.BluetoothError as exc:
            raise OSError(f"Could not look up Bluetooth service on {address}: {exc}") from exc
        if not services:
            raise ConnectionError(
                f"No '{SERVICE_NAME}' service found on {address}. "
                "Make sure the other device is running this app and advertising Bluetooth."
            )

        try:
            client = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        except bluetooth.BluetoothError as exc:
            raise OSError(f"Could not create Bluetooth socket: {exc}") from exc
        try:
            client.connect((address, services[0]["port"]))
        except bluetooth.BluetoothError as exc:
            client.close()
            raise OSError(f"Could not connect to {address}: {exc}") from exc
        except OSError:
            client.close()
            raise
        return client

    def close(self) -> None:
        self._stopping.set()
        server = self._server_socket
        self._server_socket = None
        if server is not None:
            server.close()


class _MemorySocket:
    """A socket-shaped endpoint used only by the explicit mock transport."""

    def __init__(self) -> None:
        self._incoming: queue.Queue[Optional[bytes]] = queue.Queue()
        self._peer: Optional[_MemorySocket] = None
        self._buffer = bytearray()
        self._closed = False

    def recv(self, size: int) -> bytes:
        while not self._buffer:
            item = self._incoming.get()
            if item is None:
                return b""
            self._buffer.extend(item)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def sendall(self, data: bytes) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise OSError("Mock connection is closed")
        self._peer._incoming.put(data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._peer is not None:
                self._peer._incoming.put(None)


class MockBluetoothManager:
    """A simulated echo peer for exercising the CLI without radio hardware."""

    def discover_devices(self) -> list[DiscoveredDevice]:
        return [DiscoveredDevice("MOCK-001", "Mock Echo Peer (simulated)")]

    def start_server(
        self,
        port: int,
        on_connection: Callable[[str, str, BluetoothSocket], None],
        on_error: Callable[[str], None],
    ) -> None:
        del port, on_connection, on_error

    def connect_client(self, address: str) -> BluetoothSocket:
        if address.upper() != "MOCK-001":
            raise ConnectionError(f"Mock device '{address}' was not found.")
        local, peer = _MemorySocket(), _MemorySocket()
        local._peer = peer
        peer._peer = local
        threading.Thread(
            target=self._echo_peer, args=(peer,), name="mock-peer", daemon=True
        ).start()
        return local

    @staticmethod
    def _echo_peer(peer: _MemorySocket) -> None:
        buffer = bytearray()
        try:
            while True:
                data = peer.recv(4096)
                if not data:
                    return
                buffer.extend(data)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    packet = json.loads(line.decode("utf-8"))
                    if packet.get("type") == "message":
                        reply = {
                            "type": "message",
                            "text": f"[simulated echo] {packet.get('text', '')}",
                        }
                        peer.sendall(
                            json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
                        )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        finally:
            peer.close()

    def close(self) -> None:
        pass


@dataclass
class _DeviceConnection:
    device_id: str
    name: str
    sock: BluetoothSocket
    send_lock: threading.Lock = field(default_factory=threading.Lock)


class DeviceManager:
    """Thread-safe registry and message router for active device connections."""

    def __init__(
        self,
        transport: BluetoothManager | MockBluetoothManager,
        on_message: Callable[[str, str, str], None],
        on_status: Callable[[str], None],
    ) -> None:
        self._transport = transport
        self._on_message = on_message
        self._on_status = on_status
        self._connections: dict[str, _DeviceConnection] = {}
        self._lock = threading.RLock()
        self._selected_id: Optional[str] = None

    @property
    def connected_devices(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(device_id, connection.name) for device_id, connection in self._connections.items()]

    @property
    def selected_id(self) -> Optional[str]:
        with self._lock:
            return self._selected_id

    def connect_to(self, device_id: str, device_name: Optional[str] = None) -> None:
        with self._lock:
            if device_id in self._connections:
                raise ValueError(f"Already connected to {device_id}.")
        sock = self._transport.connect_client(device_id)
        self._register(device_id, device_name or device_id, sock)

    def handle_incoming(self, device_id: str, name: str, sock: BluetoothSocket) -> None:
        try:
            self._register(device_id, name, sock)
        except ValueError:
            sock.close()
            self._on_status(f"Rejected duplicate connection from {device_id}.")

    def _register(self, device_id: str, name: str, sock: BluetoothSocket) -> None:
        connection = _DeviceConnection(device_id, name, sock)
        with self._lock:
            if device_id in self._connections:
                sock.close()
                raise ValueError(f"Already connected to {device_id}.")
            self._connections[device_id] = connection
            if self._selected_id is None:
                self._selected_id = device_id
        try:
            self._send_packet(connection, {"type": "hello", "name": name})
        except (OSError, ValueError):
            sock.close()
            with self._lock:
                if self._connections.get(device_id) is connection:
                    del self._connections[device_id]
                    if self._selected_id == device_id:
                        self._selected_id = next(iter(self._connections), None)
            raise
        threading.Thread(
            target=self._receive_loop,
            args=(connection,),
            name=f"bluetooth-recv-{device_id}",
            daemon=True,
        ).start()
        self._on_status(f"Connected to {name} [{device_id}].")

    def _send_packet(self, connection: _DeviceConnection, packet: dict[str, str]) -> None:
        data = json.dumps(packet, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME_SIZE:
            raise ValueError("Message is too long.")
        with connection.send_lock:
            connection.sock.sendall(data)

    def send_message(self, device_id: str, message: str) -> None:
        with self._lock:
            connection = self._connections.get(device_id)
        if connection is None:
            raise KeyError(f"Not connected to '{device_id}'.")
        self._send_packet(connection, {"type": "message", "text": message})

    def broadcast(self, message: str) -> None:
        with self._lock:
            connections = list(self._connections.values())
        if not connections:
            raise RuntimeError("No devices are connected.")
        errors: list[str] = []
        for connection in connections:
            try:
                self._send_packet(connection, {"type": "message", "text": message})
            except OSError as exc:
                errors.append(f"{connection.device_id}: {exc}")
        if errors:
            raise OSError("Some messages could not be sent: " + "; ".join(errors))

    def select_device(self, device_id: str) -> None:
        with self._lock:
            if device_id not in self._connections:
                raise KeyError(f"Not connected to '{device_id}'.")
            self._selected_id = device_id

    def disconnect_all(self) -> None:
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
            self._selected_id = None
        for connection in connections:
            connection.sock.close()

    def _receive_loop(self, connection: _DeviceConnection) -> None:
        buffer = bytearray()
        try:
            while True:
                data = connection.sock.recv(4096)
                if not data:
                    break
                buffer.extend(data)
                if len(buffer) > MAX_FRAME_SIZE and b"\n" not in buffer:
                    raise ValueError("Incoming Bluetooth frame exceeds the size limit.")
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    if not line:
                        continue
                    if len(line) > MAX_FRAME_SIZE:
                        raise ValueError("Incoming Bluetooth frame exceeds the size limit.")
                    self._handle_packet(connection, json.loads(line.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._on_status(f"Connection to {connection.name} ended: {exc}")
        finally:
            connection.sock.close()
            with self._lock:
                if self._connections.get(connection.device_id) is connection:
                    del self._connections[connection.device_id]
                    if self._selected_id == connection.device_id:
                        self._selected_id = next(iter(self._connections), None)
            self._on_status(f"Disconnected from {connection.name} [{connection.device_id}].")

    def _handle_packet(self, connection: _DeviceConnection, packet: object) -> None:
        if not isinstance(packet, dict):
            raise ValueError("Received an invalid message packet.")
        packet_type = packet.get("type")
        if packet_type == "hello":
            name = packet.get("name")
            if isinstance(name, str) and name:
                connection.name = name[:128]
        elif packet_type == "message":
            text = packet.get("text")
            if not isinstance(text, str):
                raise ValueError("Received a message without text.")
            self._on_message(connection.device_id, connection.name, text)
        else:
            raise ValueError("Received an unknown packet type.")


class ChatInterface:
    def __init__(
        self, transport: BluetoothManager | MockBluetoothManager, port: int
    ) -> None:
        self._transport = transport
        self._port = port
        self._events: queue.Queue[tuple[str, tuple[str, ...]]] = queue.Queue()
        self._devices = DeviceManager(
            transport,
            on_message=lambda device_id, name, text: self._post(
                "message", device_id, name, text
            ),
            on_status=lambda text: self._post("status", text),
        )
        self._discovered: list[DiscoveredDevice] = []
        self._running = True

    def _post(self, event_type: str, *values: str) -> None:
        self._events.put((event_type, values))

    def run(self) -> None:
        try:
            self._transport.start_server(
                self._port,
                self._devices.handle_incoming,
                lambda text: self._post("error", text),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Could not start Bluetooth listener: {exc}")
            self._transport.close()
            return

        print("Bluetooth Terminal Chat")
        print("Ready. Type 'help' for commands. Plain text sends to the selected device.")
        threading.Thread(target=self._input_loop, name="terminal-input", daemon=True).start()
        try:
            while self._running:
                try:
                    event_type, values = self._events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if event_type == "input":
                    self._handle_input(values[0])
                elif event_type == "message":
                    print(f"\n[Received from {values[1]} ({values[0]})]: {values[2]}")
                elif event_type in ("status", "error"):
                    print(f"\n{values[0]}")
        except KeyboardInterrupt:
            print("\nExiting.")
        finally:
            self._running = False
            self._devices.disconnect_all()
            self._transport.close()

    def _input_loop(self) -> None:
        while self._running:
            try:
                line = input("> ")
            except EOFError:
                self._post("input", "quit")
                return
            self._post("input", line)

    def _handle_input(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        command, _, remainder = line.partition(" ")
        command = command.lower()
        argument = remainder.strip()
        try:
            if command in ("quit", "exit"):
                self._running = False
            elif command in ("help", "?"):
                self._print_help()
            elif command == "discover":
                self._discover()
            elif command == "connect":
                self._connect(argument)
            elif command in ("devices", "list"):
                self._list_devices()
            elif command == "use":
                self._devices.select_device(argument)
                print(f"Sending plain-text messages to {argument}.")
            elif command == "send":
                target, separator, message = argument.partition(" ")
                if not separator or not message:
                    raise ValueError("Usage: send <device-id> <message>")
                self._devices.send_message(target, message)
            elif command == "broadcast":
                if not argument:
                    raise ValueError("Usage: broadcast <message>")
                self._devices.broadcast(argument)
            else:
                target = self._devices.selected_id
                if target is None:
                    raise RuntimeError("Not connected. Use 'discover' then 'connect <name-or-id>'.")
                self._devices.send_message(target, line)
        except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as exc:
            print(f"Error: {exc}")

    def _discover(self) -> None:
        self._discovered = self._transport.discover_devices()
        if not self._discovered:
            print("No Bluetooth devices found.")
            return
        for device in self._discovered:
            print(f"{device.name} @ {device.address}")

    def _connect(self, selector: str) -> None:
        if not selector:
            raise ValueError("Usage: connect <device-name-or-id>")
        matches = [
            device
            for device in self._discovered
            if selector.casefold() in (device.address.casefold(), device.name.casefold())
        ]
        if len(matches) > 1:
            raise ValueError("More than one device matched; use its exact address.")
        if matches:
            device = matches[0]
            self._devices.connect_to(device.address, device.name)
        else:
            self._devices.connect_to(selector, selector)

    def _list_devices(self) -> None:
        devices = self._devices.connected_devices
        if not devices:
            print("No devices connected.")
            return
        for device_id, name in devices:
            selected = " (selected)" if device_id == self._devices.selected_id else ""
            print(f"{name} @ {device_id}{selected}")

    @staticmethod
    def _print_help() -> None:
        print(
            "Commands:\n"
            "  discover                 Scan for nearby Bluetooth devices\n"
            "  connect <name-or-id>     Connect to a discovered device\n"
            "  devices                  List connected devices\n"
            "  use <device-id>          Select the target for plain-text messages\n"
            "  send <device-id> <text>  Send to a specific device\n"
            "  broadcast <text>         Send to every connected device\n"
            "  help                     Show this help\n"
            "  quit                     Exit"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal-to-terminal Bluetooth chat")
    parser.add_argument("--port", type=int, default=3, help="RFCOMM channel to advertise (default: 3)")
    parser.add_argument(
        "--transport",
        choices=("bluetooth", "mock"),
        default="bluetooth",
        help="Use RFCOMM Bluetooth or a simulated echo peer (default: bluetooth)",
    )
    args = parser.parse_args()
    transport = BluetoothManager() if args.transport == "bluetooth" else MockBluetoothManager()
    ChatInterface(transport, args.port).run()


if __name__ == "__main__":
    main()
