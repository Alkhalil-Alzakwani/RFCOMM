# Bluetooth Terminal Chat

A Python terminal chat with an RFCOMM Bluetooth transport and an explicit
in-memory mock transport for trying the CLI without Bluetooth hardware.

## Requirements

- Python 3.10+
- `PyBluez` for actual Bluetooth mode. Install a build compatible with your
  OS and Python version (`python -m pip install -r requirements.txt`).
- No third-party packages are needed for mock mode.

Bluetooth RFCOMM discovery, pairing, service advertising, and permissions are
OS-specific. Both devices must have Bluetooth enabled, be paired/allowed by
their OS as needed, and run this app. The listener advertises the
`CWI Bluetooth Chat` RFCOMM service on channel 3 by default. Some operating
systems or PyBluez builds do not support all of these operations; the app
reports setup and connection errors rather than silently switching transports.

## Run

```text
python bluetooth_chat.py
```

To try the interaction without a Bluetooth adapter or PyBluez:

```text
python bluetooth_chat.py --transport mock
```

The mock mode lists a simulated echo peer. Messages sent to it are echoed with
`[simulated echo]`; it is not a connection to another physical device.

## Commands

- `discover` scans for nearby Bluetooth devices.
- `connect <name-or-id>` connects to a device returned by discovery. You may
  also enter an address directly.
- `devices` lists active connections.
- Plain text sends to the currently selected connection.
- `use <device-id>` selects a connection for plain-text messages.
- `send <device-id> <message>` sends to one connection; `broadcast <message>`
  sends to all connections.
- `help` and `quit` show help and exit.

The terminal input thread and socket receive threads communicate through
thread-safe queues. Messages use UTF-8 JSON lines, and the device registry is
protected by a lock. `DeviceManager` supports multiple simultaneous peers.

## Tests

```text
python -m unittest -v
```
///
