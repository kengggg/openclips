# Camera hardware notes

Google Clips, model GC-6013 (2018). Everything here was observed on a
retail unit running firmware 1.8.

## Controls and LEDs

| Control | Action |
|---|---|
| Shutter button (top) | Short press wakes Myriad; in CAPTURE it requests a manual shot |
| Lens cover (slider) | **The capture switch.** Closed→open edge starts a session; closing ends it |
| Pinhole (bottom, needs a SIM tool) | Short press reboots Myriad, keeps pairing. Hold **15 s** (LED turns amber) for a factory reset |

| LED pattern | Meaning |
|---|---|
| Top and bottom white LEDs alternating | Setup mode, advertising with `setup_bit=1` |
| Centre white slow breathing | Paired and idle |
| Solid red | Crash. Short pinhole press to reboot |

* The setup window closes about 1 to 2 minutes after a factory reset. Run
  `openclips pair` while the LEDs are still volleying.
* USB-C is charge-only. No ADB, no mass storage.
* Myriad idles after a few minutes of silence even while advertising.
  If `openclips status` reports the camera asleep, press the shutter once
  and retry.
* In practice the camera answers **one GATT connection per wake**. After a
  command disconnects, the next one usually needs another shutter press.
  `openclips sync` does the whole list → Wi-Fi → download job inside one
  connection for this reason; chain your own work the same way.
* Battery drains quickly in CAPTURE. Keep it on power for long sessions.

## Internals

* **Myriad 2** vision SoC, SPARC LEON main core. Runs the protocol, the
  AES-EAX channel, the AI curation pipeline and the HTTP server on
  port 8080.
* **Sidecar** Cortex-M MCU running the BLE stack. Pure transport; it holds
  no crypto. Long or prepared ATT writes crash it.
* Wi-Fi radio brought up as a WPA2 SoftAP named `Clips6013` (channel 3
  observed) with a 16-character passphrase the camera generates.

## Linux host checklist

* BlueZ with `btgatt-client` available (package `bluez-utils` on Arch,
  `bluez` or `bluez-tools` elsewhere). Version 5.87 was used.
* Your user can open the adapter (usually the `bluetooth` group, or run
  with elevated privileges).
* NetworkManager for `openclips sync`; otherwise join the SSID by hand
  after `openclips wifi --hold`.
* Never point `bleak` or any BlueZ D-Bus GATT writer at this camera on
  Linux. It wedges the sidecar and the only cure is a factory reset,
  which also erases pairing.

## Recovering from a wedge

1. Short pinhole press. Wait for the breathing LED. Try `openclips status`.
2. Still dead: hold the pinhole 15 s until the LED goes amber. Wait for
   volleying white LEDs. `openclips forget` then `openclips pair`.
