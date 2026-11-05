import threading
import unittest

from bluetooth_chat import DeviceManager, MockBluetoothManager


class DeviceManagerTests(unittest.TestCase):
    def test_mock_connection_routes_echo_and_disconnects(self):
        transport = MockBluetoothManager()
        messages = []
        statuses = []
        received = threading.Event()
        manager = DeviceManager(
            transport,
            on_message=lambda device_id, name, text: (
                messages.append((device_id, name, text)),
                received.set(),
            ),
            on_status=statuses.append,
        )

        manager.connect_to("MOCK-001", "Mock Echo Peer (simulated)")
        manager.send_message("MOCK-001", "hello")

        self.assertTrue(received.wait(2), "timed out waiting for mock response")
        self.assertEqual(
            messages,
            [("MOCK-001", "Mock Echo Peer (simulated)", "[simulated echo] hello")],
        )
        self.assertEqual(manager.connected_devices[0][0], "MOCK-001")
        manager.disconnect_all()
        self.assertEqual(manager.connected_devices, [])


if __name__ == "__main__":
    unittest.main()
