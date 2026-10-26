# Hyxi DMU local MQTT broker

Keeps the HYXi DMU (HYX-DMU-W) "online" on a **local** MQTT endpoint instead of
the vendor cloud. This component is the broker and the responder: HA's own
asyncio loop listens on `:1883`, so there is **no Mosquitto add-on and no MQTT
integration** to run on a small VM.

## Why

The DMU dials its configured MQTT server (`GwSetInfo` section 2, normally
`fra.hyxicloud.com:1883`, plain TCP, no TLS) and publishes `registerReq` /
`uploadDataReq` requests. The cloud answers on `device/commonSvrRsp/<SN>` with
`opcode | 0x8000` and the current time. Point the DMU at HA and let this
component answer: no cloud traffic, no DNS tricks, no extra process.

## Requirements

- Port **1883** free on the HA host (stop/remove the Mosquitto add-on, and
  remove the MQTT integration if you don't use it elsewhere).
- This component in `config/custom_components/hyxi_mqtt/`.
- `configuration.yaml`:

```yaml
hyxi_mqtt:
# optional: port: 1883
```

Restart Home Assistant.

## What it does

- Accepts **anonymous** MQTT clients (the DMU's username contains a per-connect
  random, so fixed logins cannot match).
- Handles CONNECT / SUBSCRIBE / PUBLISH QoS 0+1 / PINGREQ / DISCONNECT.
- For every valid `PB` request from the DMU it publishes the matching ack on
  `device/commonSvrRsp/<SN>`:
  - `opcode | 0x8000`
  - the request sequence (field 5)
  - the current Unix time (field 6)
  - a time string in field 11 (`YYYY-MM-DD` for opcode 1, `YYYY-MM-DD HH:MM:SS`
    otherwise)

Topics logged at INFO: CONNECT, SUBSCRIBE and each acked PUBLISH.

## Push to Home Assistant

Every `device/uploadDataReq/<SN>` frame is also fired on HA's event bus as
`hyxi_dmu_push`:

```json
{"sn": "60101252400000", "topic": "device/uploadDataReq/...", "payload": "<full PB frame as hex>"}
```

`hyxi_dmu` listens for that event, decodes the list body and updates its
entities. It deliberately does **not** poll port 333: a local 333 request makes
the DMU prioritise that request and starves its own MQTT upload.

## Pointing the DMU at HA

The DMU's server address lives in `GwSetInfo` section 2, read/write **only over
the DMU's own AP** (`192.168.4.1:333`). See the `hyxi_dmu` component README for
the push architecture and protocol notes.

Server-only write (opcode `0xC022`, partial config):

```python
server = field(1, 0, 1) + field(2, 2, b"192.0.2.10") + field(3, 0, 1883)
root = field(2, 2, server)
send(frame(0xC022, field(1, 2, root)))  # reply 0x4022
```

Reboot the DMU; it connects to HA's broker. To revert, write
`fra.hyxicloud.com` / `1883` the same way.

## Notes

- The MQTT link is plaintext and the broker is anonymous: keep it on the LAN,
  never expose 1883 to the internet.
- The DMU does not appear to retry its MQTT session if the broker disappears.
  If HA restarts, the DMU may need a reboot (`0xC005` over LAN/AP) before pushes
  resume.
