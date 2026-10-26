# Jackery + HYXi local Home Assistant integrations

Local-only Home Assistant components for a Jackery SolarVault home battery,
its Smart Meter 1P CT, and the HYXi DMU microinverter gateway. No vendor cloud
is used for data or control.

## Components

| Component | Purpose |
| --- | --- |
| `custom_components/jackery_ble` | SolarVault and Smart Meter 1P over BLE (directly or through an ESPHome Bluetooth proxy), plus the follow-meter discharge supervisor. |
| `custom_components/hyxi_dmu` | HYXi DMU microinverter power/energy sensors, fed by the DMU's own MQTT uploads. Includes a restart button and an energy-statistics reset button. |
| `custom_components/hyxi_mqtt` | A small in-HA MQTT broker/ack responder for the DMU, so no Mosquitto add-on or MQTT integration is required. |

The integrations include English source strings and French UI translations.

`hyxi_mqtt` must be running for `hyxi_dmu` push updates. The DMU is configured
to use the HA host as its MQTT server; `hyxi_dmu` takes one startup snapshot
and then consumes `uploadDataReq` pushes. It deliberately does not poll port
333 periodically, because polling suppresses the DMU's upload.

The SolarVault supervisor follows the **net CT reading** at the grid feed. It
does not need the microinverter power as an input: microinverter production is already
included in the net meter measurement.

## Install

1. Copy the required folders from `custom_components/` into your HA
   `config/custom_components/` directory.
2. Add the broker to `configuration.yaml`:

   ```yaml
   hyxi_mqtt:
   ```

3. Restart Home Assistant.
4. Add the `Hyxi DMU` entry and the Jackery BLE entry through
   **Settings → Devices & services**.
5. Point the DMU's `GwSetInfo` server section at the HA broker. See
   `custom_components/hyxi_mqtt/README.md`.

The Jackery BLE integration can use a local adapter or an ESPHome Bluetooth
proxy; the probe/tooling notes are in `custom_components/jackery_ble/tools/`.

## Security

- The MQTT broker is intentionally anonymous and plaintext. Keep port `1883`
  on the LAN; never expose it to the internet.
- The DMU's setup AP uses a firmware-default password. Treat the AP as a
  configuration backdoor.
- BLE encryption keys are **not** stored in this repository. Keep your local
  `devices.json` outside the checkout and pass it with
  `--config <path>` when using the probe.
- Real device serial numbers and LAN addresses in the documentation are
  examples.

## Tests

From a Windows checkout with the `.venv` created as described in
`tools/dev/README.md`:

```powershell
.\tools\dev\run-tests.ps1
```

The script adds the small Windows shims needed by the Home Assistant test
harness; production HA runs on Linux.

## Layout

```text
custom_components/
  jackery_ble/       SolarVault + Smart Meter BLE integration
  hyxi_dmu/          HYXi DMU push integration
  hyxi_mqtt/         local MQTT broker/responder
tests/               pytest suite
tools/dev/           Windows test harness
```

## License

The source code is released under the MIT License; see `LICENSE`.

Vendor trademarks, brand assets, and reverse-engineering notes are not part of
this repository and remain the property of their respective owners.
