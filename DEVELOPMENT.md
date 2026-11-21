# Developer guide

## Supported environment

The complete adapter currently targets Linux x86-64, Node.js 22.19+ or 24, and
Python 3.11–3.13. The GitHub matrix exercises Node.js 22 and 24 and each supported
Python version. The tests simulate the installer and use loopback protocol
receivers; they do not perform a complete installation into a live ioBroker.

Windows and ARM support require changes to runtime setup and process management,
followed by platform-specific installation and playback tests.

## Layout and responsibilities

| Location                                      | Responsibility                                                             |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| `main.js`                                     | ioBroker adapter lifecycle entry point                                     |
| `lib/controller.js`                           | Own states, message commands, Admin access and optional SayIt/Alexa inputs |
| `lib/config.js`                               | Native configuration and network validation                                |
| `lib/runtime.js`                              | Instance directory, port check and isolated runtime setup                  |
| `lib/supervisor.js`                           | Python worker, bounded request/response protocol, watchdog and shutdown    |
| `worker/core.py`                              | Announcement queue, history, saved selection and cancellation              |
| `worker/airplay.py`                           | Discovery and the speech/receiver orchestration layer                      |
| `worker/pyatv_transport.py`                   | PCM scheduling and ownership of the pyatv transport lifetime               |
| `worker/sessions.py`                          | Bounded connection pool and connection reuse                               |
| `worker/pyatv_pairing.py`                     | PIN pairing through pyatv                                                  |
| `worker/pairing.py`                           | Credential persistence, validation and redaction                           |
| `worker/speech*.py`, `worker/sayit_speech.py` | Speech generation and provider-specific caching                            |
| `worker/audio_gain.py`                        | Announcement attenuation without modifying cached originals                |
| `web/`                                        | Standalone playback interface and shared frontend sources                  |
| `admin/`                                      | Admin transport, configuration editor and generated shared assets          |

The Node.js controller starts one Python worker. The worker emits newline-delimited
JSON on stdout; request identifiers link replies to commands sent on stdin. Logs
are separate JSON events. The queue, cache and AirPlay state stay in the worker.
Do not add an independent replay mechanism to the controller or supervisor.

## Configuration, state and compatibility

- The adapter namespace is `airplay-mm.<instance>`.
- Configure network interfaces, providers and preset receivers through the
  ioBroker instance native configuration. Saved selection, volume, master gain,
  cache and history remain in the instance data directory.
- The public `say` state and `speak` message command remain supported. Queue
  acceptance is distinct from successful playback; history records the result.
- Since `0.1.0-beta.2`, prototype directory imports and command/status forwarding
  to `0_userdata` have been removed. The adapter does not read sibling runtime
  directories. Obsolete native flags are ignored.
- Optional SayIt command inputs and Alexa learning are independent integrations
  and remain available. Their foreign states are read, not modified.
- Keep existing cache files and provider-specific keys intact when changing
  transports. Apply master gain to playback samples, not the cached original.
- Never log API keys, pairing secrets or PINs. Keep user input in text nodes,
  and keep pairing/cache administration restricted to ioBroker Admin.

## pyatv is intentionally pinned

The sender uses **pyatv 0.18.0**. Runtime pins are in
`worker/setup_runtime.py:PACKAGES`; software-test pins are in
`requirements-test.txt`. Update both together.

The transport uses pyatv internals rather than only its public facade:

- RAOP `StreamContext`, `StreamClient`, `AudioProtocol` and `AudioSource`;
- the AirPlay 1/2 protocol classes and RTSP/HTTP helpers;
- `StreamClient._stream_data` for continuous packet transmission;
- timing, feedback and connection objects whose lifecycle must be closed even
  after a failed RTSP teardown.

`AnnouncementSource` supplies silence between announcements and schedules each
clip against the sender sample clock. `PyatvSession` owns startup, playback and
cleanup. NTP timing is supported; PTP is not implemented. Group scheduling is
not a promise of sample-accurate acoustic synchronization across receivers.

Do not perform an unattended pyatv dependency upgrade. A new internal API can
change authentication, packet timing or cleanup without changing the public
facade. The `srptools==1.0.1` pin is also deliberate: SRP behavior is exercised
by the pairing/protocol tests.

For a dependency update:

1. Review the upstream changes to every internal class used by the transport.
2. Update the runtime and test pins together, including applicable license notices.
3. Run the complete software test suite and the full supported CI matrix.
4. Inspect the targeted transport tests listed below for adequate coverage.
5. On a separate test installation, check fresh setup, PIN pairing where needed,
   first and repeated playback, disconnect/reconnect and shutdown with a receiver.
6. Check that speech files, cache identifiers, volume and master gain are preserved.

The focused tests are `test_connection.py`, `test_pyatv_transport.py`,
`test_pyatv_hap.py`, `test_pyatv_groups_wire.py`, `test_sessions.py` and
`test_idle_stop.py`. They cover failed setup, incomplete timing, authenticated
wire exchange, sample delivery, cancellation, group isolation and socket cleanup.

## Editing and validation

```sh
npm ci --ignore-scripts
npm run format
npm run check
npm test
python -m pip install -r requirements-test.txt
npm run test:python
```

`npm run format` formats the repository and regenerates Admin assets.
`npm run format:check` verifies formatting without writing files. Copied licenses
and upstream validation schemas are excluded from formatting.

Edit `web/app.js`, `web/app.css`, `web/device-icons.js`, `web/index.html` and
`admin/settings.html` as the relevant source files. `tools/build-admin.mjs`
generates `admin/index_m.html` and copies the shared JavaScript/CSS assets.
Regenerate these outputs before committing; do not maintain two independent
versions of the shared playback interface.

JavaScript tests use injected adapter/worker fixtures. Installer tests use an
isolated process environment and simulated Python responses. Python tests use
local temporary data and loopback receivers. They do not require real speakers,
download voice models, install Piper or contact a cloud speech service.

Hardware checks should be deliberate and separate from automated tests. Retain
the previous release and instance data before experimenting with a real setup.
