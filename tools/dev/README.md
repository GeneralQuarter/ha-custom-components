# Run the test suite (Windows)

Home Assistant only supports Linux, so the test harness needs three small
stand-ins from `tools/dev/windows_shims` before `pytest-homeassistant-custom-component`
can even import:

| Shim | Why |
|---|---|
| `fcntl.py` | `homeassistant.runner` imports it at plugin load time |
| `resource.py` | `homeassistant.util.resource` imports it |
| `aiousbwatcher.py` | inotify C extension pulled in by `components.usb`, which `components.bluetooth` imports |

The shims are only reachable from setup paths the integration never uses: it
talks to a Bluetooth *proxy* over TCP, so there is no local adapter, no USB
watcher and no PID-file locking.

`tests/conftest.py` covers the two remaining Windows gaps:

- `socket.socketpair` is reinstated for asyncio's self-pipe, because
  `pytest-socket` blocks the socket factory it uses on Windows.
- the local adapter history scan is stubbed, because
  `pytest-homeassistant-custom-component` mocks the Linux adapter list but not
  the D-Bus history lookup behind it.

## Usage

```powershell
# from the repository root
.\tools\dev\run-tests.ps1                 # whole suite
.\tools\dev\run-tests.ps1 tests/test_protocol.py -k encryption
```

The script puts the shim directory on `PYTHONPATH` and runs the venv's pytest,
so no environment variable has to be set by hand.

## Environment

Created with `uv`, Python 3.14 (the version Home Assistant 2026.10 requires):

```powershell
$env:UV_LINK_MODE = 'copy'
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r tools\dev\requirements-dev.txt
```

`pytest-homeassistant-custom-component==0.13.373` pins
`homeassistant==2026.10.2`, the same version the production instance runs.
Bump both together and re-run the suite.
