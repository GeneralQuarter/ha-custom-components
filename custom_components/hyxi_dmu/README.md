# Hyxi DMU (local)

Exposes the HYX-DMU-W's microinverter power and lifetime energy in Home
Assistant using the DMU's **own MQTT uploads**, with no vendor cloud and no
account.

## Data path

1. `hyxi_mqtt` runs a local broker on `:1883` and answers the DMU's requests.
2. The DMU publishes `device/uploadDataReq/<SN>` about once per minute.
3. `hyxi_mqtt` forwards each frame as the HA event `hyxi_dmu_push`.
4. This integration decodes the frame with the same parser used for the local
   `0x4003` list reply and updates the sensors.

There is **no periodic 333 poll**. A local request makes the DMU prioritise the
333 client and starve its own MQTT upload, so polling would stop the push. The
coordinator takes one snapshot when the entry starts (useful immediately and
harmless as a one-off), then waits for pushes.

## Energy statistics

`sensor.hyxi_dmu_microinverter_energy` is a lifetime `TOTAL_INCREASING` counter. It
reports **unknown** when there is no snapshot, never `0`; a fake zero would look
like a counter reset to the Energy dashboard and re-add the whole lifetime
total. If an older version already polluted the statistics, press **Reset energy
statistics** on the DMU device once. It clears the microinverter total's long-term
statistics through the recorder API; the dashboard then rebuilds from a clean
baseline. (The same action is available in **Settings → Tools → Statistics**,
but the button is easier.)

## Restart button

The **Restart** button logs in on port 333 and sends `0xC005`. It retries if
the local service does not answer the login (the DMU can stall on 333 for
10–15 s, especially after the broker disconnects). The DMU may take 3–4 minutes
to reboot and reconnect. Use it after an HA restart if the DMU has not
reconnected to the local broker.

Attempt results are appended to `/config/hyxi_dmu_restart.log`; no credentials
are written.

## Configuration

- **Host**: the DMU's LAN address. Give it a DHCP reservation first.
- **Password**: the local password from the HYXiPOWER app. The DMU currently
  accepts any value, but the handshake must be sent.

The config flow verifies the address with `0xC002`. To use push, enable the
`hyxi_mqtt` component and point the DMU's `GwSetInfo` server section at HA:

```yaml
hyxi_mqtt:
```

See `custom_components/hyxi_mqtt/README.md` for the broker setup and the
server/config write.

## Protocol notes (observed)

```
frame    "PB" | len(BE16) | CRC16/MODBUS(BE16) | protobuf
request  1=session, 2=channel, 3=opcode, 6=unix time, 11=inner
login    0xC022 (once per connection)  ->  0x4022
list     0xC003 on channel 0x201       ->  0x4003
upload   0x0003 on MQTT                ->  0x8003 ack
```

`0x4003` / upload list layout:

```
11: { 7: { 1: {1: SN, 7: rated W}
           3: {1: MPPT index, 2: volts*10, 4: watts*10, 5: energy}   (repeated)
         }
      ...
    }
```

- Power per MPPT = field 4 / 10 W (DC; AC is ~5 % lower)
- Voltage = field 2 / 10 V
- Lifetime energy = field 5 × 0.01 kWh

## Caveat

If HA restarts, the broker drops the DMU's MQTT session. The DMU does not
appear to retry on its own; a C005 restart (over the LAN or the DMU AP) brings
the push back.
