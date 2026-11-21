# AirPlay MM

An ioBroker adapter for text-to-speech announcements on AirPlay receivers,
including HomePod and other compatible speakers. Generate speech locally with
Piper, through SayIt, or with Speechify, and reuse it from a persistent cache.

**Public beta for testing and community development.** The complete adapter
currently targets Linux x86-64. Receiver compatibility depends on the device
and its firmware; review the requirements and network notes before installing.

## Features

- AirPlay 1 and AirPlay 2 transport and PIN pairing using pyatv.
- Persistent connections and announcements to up to eight receivers.
- Local Piper voices: Thorsten and Ramona (German).
- Optional SayIt/Google speech and Speechify Runa, with local Ramona fallback.
- A speech cache with individual playback and deletion controls.
- Separate receiver volume and announcement master gain.
- Adjustable silent pre-roll, receiver discovery and announcement history.
- Configuration and playback controls integrated into ioBroker Admin.

## Requirements

- Linux x86-64. The full adapter does not currently support Windows or ARM.
- Node.js 22.19 or later in the Node 22 series, or Node.js 24.
- Python 3.11, 3.12 or 3.13, including the `venv` module.
- ioBroker js-controller 6.0.11+ and Admin 7.0.23+.
- AirPlay receivers on a reachable local network.
- Internet access for initial runtime and voice downloads.

Piper runs offline after setup. Speechify and SayIt/Google need internet for
texts that are not cached. Speechify requires your own API key. Dependencies
and models are installed into the instance data directory; no voice models or
account credentials are bundled with the adapter.

## Installation

Download the [0.1.0-beta.2 package](https://github.com/mermayer/ioBroker.airplay-mm/releases/download/v0.1.0-beta.2/iobroker.airplay-mm-0.1.0-beta.2.tgz)
from [GitHub Releases](https://github.com/mermayer/ioBroker.airplay-mm/releases).
In ioBroker Admin, use custom-source installation and supply the package URL.
The source code and release downloads are public and require no GitHub invitation
or access token. This beta is distributed through GitHub; inclusion in the
official ioBroker adapter repository is a separate process.

To build an installation archive from source on a supported Linux host:

```sh
git clone https://github.com/mermayer/ioBroker.airplay-mm.git
cd ioBroker.airplay-mm
npm ci --ignore-scripts
npm run build
npm pack --ignore-scripts
```

Install the resulting `.tgz` through ioBroker Admin's custom-source workflow.

## Quick start

1. Install the adapter using ioBroker Admin and create an instance.
2. Open its configuration, select the local network interface and save.
3. Select a speech provider. Enter your Speechify API key in its dedicated
   field, or install `sayit` on the same host to use SayIt/Google.
4. Start the instance and wait for initial setup to finish. Progress appears in
   the ioBroker log and the `info` states.
5. Open **Speech output**, choose **Search**, select a receiver and set a volume.
6. If pairing is required, choose **Pair** and enter the PIN from the receiver.
7. Enter text and choose **Start announcement**. **Prepare text** generates and
   caches speech without playing it.

## Announcements from ioBroker scripts

Use the saved receiver selection and volume:

```javascript
setState('airplay-mm.0.say', 'The window is open.');
```

Specify a receiver using its ID from the receiver list:

```javascript
sendTo(
  'airplay-mm.0',
  'speak',
  {
    text: 'The announcement is ready.',
    target: 'receiver-id',
    volume: 20,
  },
  (result) => {
    if (!result.ok) log(result.error, 'warn');
  },
);
```

For multiple receivers, supply `targets` instead of `target`:

```javascript
sendTo('airplay-mm.0', 'speak', {
  text: 'Please close the windows.',
  targets: ['receiver-id-1', 'receiver-id-2'],
  volume: 20,
});
```

Requests accept up to 500 characters and eight distinct receivers. Volume must
not exceed the configured limit. An optional `requestId` identifies duplicate
requests. Queue acceptance is not a playback confirmation; check the history
for the final result. Failed or interrupted announcements are not replayed
automatically.

## Volume, cache and backups

**Receiver volume** changes the AirPlay receiver level. **Announcement master
gain** attenuates the announcement samples before transmission. It does not
alter cached files or the volume of Siri and music. A gain of 100 preserves the
original signal, 50 halves its amplitude, and 0 produces silence.

```javascript
setState('airplay-mm.0.masterGain', 50);
```

The cache distinguishes texts, voices and providers. Existing files are reused;
new texts are generated and cached. Speechify falls back to Ramona if online
generation fails. Keep the `airplay-mm.<instance>` data directory to preserve
cached audio, history, settings and pairings. It is registered for ioBroker
backups. A restored runtime may need rebuilding on different hardware.

## Network and compatibility

Receivers must be able to send UDP timing packets back to the ioBroker host.
An outgoing TCP connection alone is not enough. If setup reports missing timing
requests, check the host firewall and VPN. The adapter does not change firewall
rules or network profiles.

Receiver support depends on firmware and AirPlay access settings. This beta
uses NTP; PTP is not supported. A common scheduled start does not guarantee
sample-accurate acoustic synchronization. Persistent connections can occupy
the receiver AirPlay input until released or taken over by another sender.

Use the standalone interface only on a trusted local network. Do not expose its
port to the internet. Keep instance data and backups private.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for the module layout, the intentionally
pinned pyatv internals, and the dependency update procedure.

```sh
npm ci --ignore-scripts
npm run format
npm run build
npm run format:check
npm run check
npm test
python -m pip install -r requirements-test.txt
npm run test:python
```

Automated tests use isolated fixtures and local protocol receivers. They do not
contact real speakers, require cloud accounts, install Piper or download models.
CI covers Node.js 22 and 24, and Python 3.11–3.13.

## Contributing and reporting problems

Fork the repository and submit changes as a pull request. Use
[GitHub Issues](https://github.com/mermayer/ioBroker.airplay-mm/issues) to report
problems, including the adapter version, operating system, Node.js/Python
versions, receiver model and relevant sanitized logs. Remove API keys, pairing
credentials, personal announcement text and private network details from reports.

See the developer guide before changing the pinned transport dependencies.

## License and acknowledgements

Copyright (c) 2026 mermayer. Licensed under **GPL-3.0-or-later**; see
[LICENSE](LICENSE) and [NOTICE.txt](NOTICE.txt).

AirPlay transport uses [pyatv](https://github.com/postlund/pyatv) under MIT.
Local speech uses [Piper](https://github.com/OHF-Voice/piper1-gpl) under GPL.
Other dependencies retain their own licenses. Notices are included in
[licenses](licenses) and in the installed dependencies. Voice models are
downloaded separately and retain their respective model and dataset terms.
Speechify is an optional hosted API subject to its account terms; no Speechify
code or models are distributed.

Apple, AirPlay and HomePod are trademarks of Apple Inc. This project is
independent and is not endorsed or certified by Apple.
