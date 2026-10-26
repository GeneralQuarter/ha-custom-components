# Jackery BLE probe

Standalone probe for the Jackery SolarVault and Smart Meter 1P. It talks to the
devices **only** through an ESPHome Bluetooth proxy; no cloud, MQTT or Jackery
app is involved.

The probe is a development tool. It is not imported by the Home Assistant
integration at runtime.

## Install

Use a virtual environment so the integration's dependencies are not mixed with
the probe's:

```powershell
python -m venv .venv-probe
.venv-probe\Scripts\Activate.ps1
python -m pip install -r jackery_ble/tools/requirements-probe.txt
```

`bleak-esphome` uses the ESPHome API port `6053`, not the web UI port. The proxy
must have `api:` enabled and `bluetooth_proxy:` active.

## Device config (address + key)

Keys are **not** stored in this repository. Keep a local `devices.json`
outside the checkout and pass it with `--config <path>`. On the first run
the probe creates a local template with example addresses and empty keys:

```json
{
  "devices": {
    "vault": {"address": "AA:BB:CC:DD:EE:01", "key": "", "format": "home"},
    "ct":    {"address": "AA:BB:CC:DD:EE:02", "key": "", "format": "accessory"}
  }
}
```

Fill in each `key` (base64 or hex, exactly as the Jackery app shows it) and the
prompt disappears. `--device` picks an entry and sets its address, key and frame
format in one go:

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 --device vault
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 --device ct
```

Check it without touching a device (keys are never printed):

```powershell
python custom_components/jackery_ble/tools/probe.py --config C:\Users\<you>\AppData\Local\Jackery\devices.json --list-devices
```

```
Config: C:\Users\<you>\AppData\Local\Jackery\devices.json
  vault        AA:BB:CC:DD:EE:01  home       key set (24 chars)
  ct           AA:BB:CC:DD:EE:02  accessory  NO KEY
```

Order of precedence: `--key` / `--address` on the command line, then the config,
then the hidden prompt. Use `--config <path>` for a different file. Keep that
file out of version control and backups you share: it holds the Bluetooth keys
for both devices. Only the path is written to the log, never a key.

## Scan and pick a device

From the repository root:

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20
```

The probe scans for advertisements, prints address/RSSI/SN/category/model and
the raw manufacturer/service data, then asks which device to use. If nothing
matches the Jackery signature it shows every device. Use `--all` to force that
behavior. When you pick an address that is in `devices.json`, its key is used
automatically.

To check the proxy and see devices without entering a key:

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 --scan-only --all --scan-timeout 30
```

The Bluetooth key is requested with hidden input unless the config (or `--key`)
provides it. It is never written to the log. Prefer the config over `--key`,
which puts the key in your shell history.

## Read a device

```powershell
# CT meter (accessory frame dialect first, then the home dialect)
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 --device ct

# SolarVault (reads 3011/106 and 3019/120)
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 --device vault
```

The probe tries the plausible action/header combinations. For an accessory
device it sends both `106` and `120` for each pair and stops after the first
pair that answers. For the vault it sends the device-property (`3011/106`) and
combine-data (`3019/120`) reads.

If the device ignores the first frame, add `--time-sync` to send the app's
time-zone frame (`3003/3` for the vault, `4003/3` for accessories) before the
reads.

Each run writes `jackery_ble/tools/logs/probe-YYYYmmdd-HHMMSS.log` with raw
frames and a dotted field summary. The console shows the decoded JSON.

## Bind the CT to the vault

The known local bind command is action `3012`, BLE cmd `108`, body
`{"sn": "<CT_SN>"}`. It must be sent **to the SolarVault**, not to the CT.

The probe can do baseline read → bind → poll in one run:

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 `
    --device vault `
    --bind-sn CT-SERIAL --bind-wait 45
```

It reads `3011/106` and `3019/120` first (so `ctStat` has a baseline), sends the
bind, then polls `3019/120` every 2 s and logs `ctStat`, `ctState`,
`inGridSidePw`, `outGridSidePw` and `otherLoadPw` until the value changes or
`--bind-wait` expires. Add `--follow-meter` to also send `workModel=4`
(`3027/121`) and `isFollowMeterPw=1` (`3044/121`) afterwards.

### Did the write even apply?

The vault **does answer writes**: `3029/121 {"maxFeedGrid":700}` came back in
about 120 ms as an echo (`action=3029 cmd=121 {"cmd":121,"maxFeedGrid":700}`)
plus a push carrying the same value. A silent write is therefore meaningful,
not normal. Use `--verify-key` to confirm the value actually moved:

```powershell
# Set the export cap to 700 W and read it back
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 `
    --device vault `
    --action 3029 --cmd 121 --body '{"maxFeedGrid":700}' `
    --verify-key maxFeedGrid

# Restore it
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 `
    --device vault `
    --action 3029 --cmd 121 --body '{"maxFeedGrid":800}' `
    --verify-key maxFeedGrid
```

`verify: maxFeedGrid changed 800 -> 700` means the command path works and a
failed bind is a device-side problem (range, missing accessory key, app gating).
`verify: maxFeedGrid unchanged (800)` means the write itself is not being
applied and the command framing needs work first.

**Result 2026-10-23:** `3029/121` applied (`800 -> 700`) and was echoed.
`3012/108 {"sn":"HW2C000000002"}` produced **no reply at all** and `ctStat`
stayed `0` for 60 s. The app does define BLE type `108` for `BIND_SMART_PART`,
so the firmware is meant to accept it; the likely failure is that the vault
could not reach the CT at ~10 m through a wall (-77 dBm at the proxy), and the
bind result is only reported once that attempt resolves.

Manual equivalent, if you prefer raw writes:

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 `
    --device vault `
    --action 3012 --cmd 108 --body '{"sn":"CT-SERIAL"}'
```

Then poll the vault's `120` data for `ctStat=1` and populated
`inGridSidePw`/`outGridSidePw`. Unbind is action `3013`, cmd `109`, with the
same body.

Once the CT is bound, enable the native follow-meter loop:

```powershell
# Replace ... with: --proxy 192.0.2.20 --address <VAULT_MAC> --format home

# Custom work mode
python custom_components/jackery_ble/tools/probe.py ... --action 3027 --cmd 121 --body '{"workModel":4}'

# Follow meter power
python custom_components/jackery_ble/tools/probe.py ... --action 3044 --cmd 121 --body '{"isFollowMeterPw":1}'
```

## Custom-mode power schedules (the only >200 W actuator)

Facts taken from the decompiled app (`HomeCmdAction`, `HomeDeviceController`,
`EditCustomPlanActivity`, `CustomModeTask`):

| Item | Value |
| --- | --- |
| `workModel` | 2 self-use, 3 battery priority, 4 custom, 5 time-of-use, 7 dynamic price, 8 smart |
| `defaultPw` (`3043/121`, "default output power") | 0-200 W in the app UI, so **not** the 2500 W actuator |
| schedule family | `3015/112` add, `3016/112` delete, `3017/112` update, `3018/112` read |
| schedule body | `actionType`, `taskType` (2 = custom mode), `tid`, `start` `"HH:mm"`, `end` `"HH:mm"`, `loops` (7 day flags, Monday first), `mode` (1 = charge, 2 = discharge), `pw`, `sysSwitch` |
| plan power limit | `maxSysInPw` for charge, `maxSysOutPw` for discharge (2500 W on a 3 Pro Max) |

The vault then runs the plan itself, so a plan is a minute-granular setpoint,
not a real-time loop. Read the current list first:

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 `
    --device vault --schedule read
```

Add a discharge plan (00:00-23:59 keeps timezone out of the test):

```powershell
python custom_components/jackery_ble/tools/probe.py --proxy 192.0.2.20 `
    --device vault `
    --schedule add --schedule-pw 500 --schedule-mode discharge
```

The probe logs the `tid` it generated and re-reads the list afterwards. Change
or remove the plan with that id:

```powershell
--schedule update --schedule-tid <TID> --schedule-pw 1200
--schedule delete --schedule-tid <TID>
```

Leave a plan in place and the vault will keep discharging until `end` or until
the battery hits `socDischgLimit` (33 % here, with `soc` at 31 %). Always delete
or disable a test plan when you are done.

`SET_CHARGE_DISCHARGE_LINE` (3028) and `CONTROL_MAX_OUT_PW` (3038) have
`bleMsgType = 0` in the app, i.e. they are cloud/MQTT only: the SOC limits and
the output cap cannot be changed over BLE.


## Smart Meter 1P: confirmed behavior (2026-10-22)

Observed against `AA:BB:CC:DD:EE:02` (SN `HW2C000000002`, model 4002) through
the proxy. See `logs/probe-20261022-225334.log` for the raw frames.

- GATT layout is the same as the vault: write `ee01`, notify `ee02`,
  `MTU=517`.
- **The CT pushes telemetry on its own** every ~2 s (`cmd=107`), starting
  immediately after `start_notify`, before any write. The `action` field of a
  push is a rolling sequence number (observed 4, 5, 6, ...), not a command ID.
- Every notification is sent **twice**, byte-for-byte identical, ~405 ms
  apart. Consumers must de-duplicate.
- `4005 / cm106` is answered (echoed action `4005`, body with `volt`, `curr`,
  `power`, `freq`, `fact`, `ap`). `3011 / 106` is not answered.
- `3019 / 120` is **not supported**: the device ignores it and keeps pushing
  `107`. Do not use it on the CT.
- Push frames are partial: `volt`/`curr`/`freq`/`ap` are only present in some
  frames, and `rep` only in others. Merge fields across frames instead of
  replacing the whole payload.
- No SN, no `ctStat`, no lifetime energy counters in the CT payload; the CT is
  a live-measurement-only device.

Wire units, verified against `ap / volt` and `power / ap`:

| field  | meaning          | wire value | unit        |
| ------ | ---------------- | ---------- | ----------- |
| `volt` | voltage          | 233        | V           |
| `curr` | current          | 25         | 0.1 A       |
| `power`| active power     | 497        | W           |
| `freq` | frequency        | 50         | Hz          |
| `fact` | power factor     | 857        | 0.001 (0.857) |
| `ap`   | apparent power   | 606        | VA          |
| `rep`  | reactive power   | -104       | var (see note) |

Open questions for the next session:

- Sign of `power` (import vs export) is unconfirmed; all samples were positive
  while the house was importing ~500 W.
- `rep` does not reconcile with `sqrt(ap^2 - power^2)` (~320 var expected,
  ~104 reported). Treat it as diagnostic until the firmware definition is
  known.

## Troubleshooting

- **No devices found**: confirm `api:` and `bluetooth_proxy:` in the proxy YAML,
  that the proxy is online, and that Home Assistant is not already using all
  proxy connection slots. The ESPHome proxy has a small number of simultaneous
  BLE connections.
- **Device found but connect fails**: move the proxy closer, or use
  `--scan-timeout 30`. Check that the device is not connected to the Jackery app
  on a phone.
- **Notifications arrive but do not decrypt**: the key is wrong or the firmware
  uses a different key. The log contains the raw frame; do not share it
  publicly if it contains a serial number.
- **No response to writes**: try `--wire base64` for older firmware, or a
  different `--chunk-size` (for example `180`).
- **`APIConnectionManager` timeout**: the proxy may have API encryption enabled.
  Pass its base64 `noise_psk` via `--proxy-key`.
