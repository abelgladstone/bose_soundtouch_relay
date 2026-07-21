# Bose SoundTouch 300 — cross-VLAN relay: findings so far

## Problem

The Bose SoundTouch 300 soundbar cannot be reached from devices on a different VLAN,
even when mDNS/Bonjour discovery *does* work across VLANs. This breaks:

- **AirPlay** from phones/tablets to the speaker (device is discoverable, connection never
  completes).
- **Spotify Connect** re-pairing (same symptom — discoverable, pairing handshake never
  completes).
- Anything else that needs a direct TCP connection to the speaker from outside its own
  subnet.

This is **not a firewall/routing problem** — see root cause below. It's unrelated to the
`bose_st300` web remote app itself (that app already works fine, because it happens to run
on the same subnet as the speaker today).

## Network topology

- **VLAN1** — trusted network: phones, tablets, everything the household uses directly.
- **VLAN2** — service network: mostly IoT / Home Assistant devices. The Bose SoundTouch 300
  lives here. The Ubuntu Server (currently running *only* the `bose_st300` web remote) also
  lives here.
- Router/AP stack: **UniFi**.
- Proxmox host: a Beelink box with **two physical Ethernet ports** (only one currently in
  use). This is available for a dual-homed relay VM/LXC if needed — see "Open questions"
  below, deferred to the new project.

## Diagnostic steps taken and results

All tests below were run from a host on VLAN1 (198.51.100.0/24) against the speaker, which
mDNS-discovers at `192.0.2.20` (VLAN2, "Zola").

1. **mDNS discovery works across the VLAN boundary.** Both `_soundtouch._tcp.local.` and
   `_spotify-connect._tcp.local.` announcements from the speaker are visible from VLAN1.
   This strongly suggests **UniFi is reflecting mDNS multicast across VLANs** (a common
   UniFi feature/setting), independent of whether unicast routing exists.

   Discovered services on the speaker (`192.0.2.20`):
   ```
   _soundtouch._tcp.local.       Zola             192.0.2.20:8090
     DESCRIPTION=SoundTouch, MAC=AABBCCDDEEFF, MANUFACTURER=Bose Corporation, MODEL=SoundTouch

   _spotify-connect._tcp.local.  Zola             192.0.2.20:8200   CPath=/zc, VERSION=1.0
   _spotify-connect._tcp.local.  Example           192.0.2.20:8200   CPath=/zc, VERSION=1.0
   _spotify-connect._tcp.local.  Zola (test)      192.0.2.20:8200   CPath=/zc, VERSION=1.0
   _spotify-connect._tcp.local.  Arnim Zola       192.0.2.20:8200   CPath=/zc, VERSION=1.0
   ```
   The four differently-named Spotify Connect entries on the same IP:port line up with the
   multiple Spotify account slots already visible in the SoundTouch `/sources` API response
   (`spotify_user`, `SpotifyConnectUserName`, `SpotifyAlexaUserName` — captured in
   `bose_st300/tests/fixtures/sources.xml`). Port **8200**, path **`/zc`**, is the speaker's
   Spotify Connect Zeroconf pairing endpoint (`getInfo`/`addUser` — the standard Spotify
   Connect local-pairing protocol, which is normally vendor-cloud-independent).

2. **Unicast TCP does not cross the VLAN boundary to the speaker.** Direct TCP connection
   attempts from VLAN1 to VLAN2 all failed (timeout, no SYN-ACK):
   ```
   192.0.2.20:8090  (SoundTouch HTTP API)         NOT reachable
   192.0.2.20:8080  (gabbo WebSocket push feed)    NOT reachable
   192.0.2.20:8200  (Spotify Connect Zeroconf)     NOT reachable
   192.0.2.20:5000  (AirPlay)                      NOT reachable
   192.0.2.20:7000  (AirPlay/RAOP)                 NOT reachable
   ```

3. **Root cause, per direct experience: it's the speaker's own network stack, not UniFi
   firewall/routing config.** The user already ruled out a UniFi firewall rule as a fix —
   the speaker's embedded network stack does not reply correctly to hosts outside what it
   considers its own local subnet, regardless of whether routing/firewall rules would
   otherwise permit the traffic. (Symptom matches a common embedded-device bug: the device
   only replies to peers it perceives as on-link, e.g. due to a naive subnet check, and
   silently drops anything else — packets arriving are not necessarily rejected, replies
   just never come back correctly.)

   Supporting evidence: the `bose_st300` app, deployed on the Ubuntu Server which sits on
   **VLAN2 with the speaker (same subnet)**, already works today — proving same-subnet
   communication with the speaker is completely fine. It's specifically *cross-subnet*
   unicast to the speaker that fails, for every protocol tested (SoundTouch API, gabbo WS,
   Spotify Connect Zeroconf, AirPlay).

4. **Options considered and why they were set aside:**
   - *UniFi firewall rule allowing VLAN1 → speaker's IP*: rejected — routing/firewall isn't
     the blocker, the speaker's stack is.
   - *Move the speaker (and server) physically/logically onto VLAN1*: technically works
     (removes the cross-subnet hop entirely) but mixes an IoT device into the trusted
     network, and doesn't generalize — reserved as a fallback, not the chosen path.
   - *Reimplement the Spotify Connect Zeroconf pairing protocol server-side (in the
     `bose_st300` app itself)*: would only fix Spotify re-pairing, not AirPlay or any other
     protocol needing sustained cross-VLAN connectivity. Too narrow.
   - **Chosen direction: a small dual-homed relay ("thin client") service** — see below.

## Chosen direction: dual-homed relay service

Build a standalone relay that sits with one leg on VLAN1 and one leg on VLAN2 (same subnet
as the speaker), and transparently bridges **all** local protocols to/from the speaker:

- **mDNS impersonation**: on its VLAN1-facing interface, answer for the speaker's services
  (`_soundtouch._tcp`, `_spotify-connect._tcp`, `_airplay._tcp`/`_raop._tcp`, and any others
  discovered) advertising **its own VLAN1 address**, not the real speaker's VLAN2 address —
  mirroring the real service names/TXT records (captured above) so nothing looks different
  to clients.
- **TCP relay**: for each connection a VLAN1 client makes to the relay on one of those
  ports, open a corresponding connection to the real speaker on VLAN2 (same-subnet, so the
  speaker's broken cross-subnet behavior never comes into play) and pipe bytes both ways.
- **Everything talks through the relay** — including the `bose_st300` web remote itself
  (`SoundTouch_ui`), not just phones/tablets. The relay becomes the single point of contact
  for the speaker; nothing talks to the speaker's real VLAN2 IP directly except the relay.

This is intentionally scoped as **a separate project from `bose_st300`** — it's a generic
network appliance (mDNS + TCP relay for a misbehaving embedded device), not specific to the
web remote's UI/control logic.

## Relevant code in `bose_st300` (for reference, not to be modified by the new project)

- `soundtouch/discovery.py` — existing mDNS discovery of the speaker over
  `_soundtouch._tcp.local.`, using the `zeroconf` Python package (`ServiceBrowser`,
  `ServiceListener`). Good reference/reusable pattern for the relay's own mDNS handling
  (both for browsing the real speaker's records and for impersonating them on VLAN1).
- `soundtouch/api.py` — `SoundTouchClient`, talks to the speaker's REST API on port 8090.
  Once the relay exists, this client's target host should become the relay's address, not
  the speaker's real address.
- `soundtouch/notify.py` — `NotificationBridge`, connects to the gabbo WebSocket push feed
  on port 8080. Same relay consideration applies.
- `config.toml` / `BOSE_HOST` env var (see `soundtouch/discovery.py:Config.load`) — this is
  where `bose_st300` would eventually be pointed at the relay's address instead of doing its
  own mDNS discovery of the real speaker, once the relay exists.
- `tests/fixtures/sources.xml` — real captured `/sources` XML showing the three Spotify
  account slots referenced above.

## Decisions

- **Where the relay runs**: on the existing Ubuntu Server, in its own container — not a new
  separate VM/LXC. The Ubuntu Server currently only runs `bose_st300`, so it has spare
  capacity; the container needs two interfaces (one on VLAN1, one on VLAN2 alongside the
  speaker) to act as the dual-homed relay. Exact container tech (Docker/Podman/LXC nested
  under the VM, etc.) still open — `bose_st300` itself already ships a `Dockerfile` /
  `docker-compose.yml`, which may be a useful reference for however this server is
  provisioned.
- **Python tooling**: use `uv` for the Python side, matching every other project on this
  machine (`bose_st300`, `ha_solax_zaptec`, `kyrkdmx`, `tou_optimizer` — see
  the workspace CLAUDE.md).
- **Testing**: Python's built-in `unittest` (not pytest) — matching `bose_st300`'s
  `tests/` (run via `uv run python -m unittest discover tests/ -v`).

## Open questions for the new project (not yet decided)

1. **Implementation style**:
   - Custom Python service doing both mDNS impersonation and TCP proxying together
     (reuses the `zeroconf` dependency already familiar from `soundtouch/discovery.py`,
     self-contained, easier to debug/extend to new services).
   - vs. iptables DNAT/SNAT for the data-plane (kernel does the relaying, less custom code)
     plus a minimal custom mDNS-rewriting helper (still needed either way, since plain mDNS
     reflection advertises the real, unreachable speaker IP).
3. **Full protocol/port list to relay**: at minimum 8090 (SoundTouch API), 8080 (gabbo WS),
   8200 (Spotify Connect Zeroconf, path `/zc`). AirPlay needs its own port survey (5000 and
   7000 were tested and found closed cross-VLAN, but AirPlay 2 / RAOP may use additional
   ports, e.g. mDNS-advertised port varies, plus possible UDP for timing/control — needs
   checking against the real `_airplay._tcp`/`_raop._tcp` TXT records once designing the
   relay).

## Superseded: "AirPlay is a dead end" — the zero-relay test never exercised the real gap

The section below was the working conclusion for a while and is kept for the record, but
it was wrong, and the mistake is instructive: the "zero relay involved at all" `pyatv` test
described below must have been run **on the speaker's own subnet** — cross-subnet raw TCP
doesn't work at all per finding #2 above, so a same-subnet client's dynamic-port connection
succeeds trivially (it's on-link, just like everything else about this speaker) and the test
never actually exercised the relay's cross-VLAN gap. It proved the control channel handshake
itself is fine, not that the dynamic port is unfixable.

A live `tcpdump` capture of a real cross-VLAN attempt (iPhone on VLAN1, relay + speaker on
VLAN2, taken after the `NameError` fix above) showed the control channel (port 7000)
completing a full pair-setup/pair-verify exchange through the relay cleanly, then the client
opening a new connection to a second, dynamically-numbered port and getting an immediate
`RST` — nothing was listening there. That's gap #1 below, and it's the *whole* problem, not
one of two.

The obvious fix -- have the relay scan the speaker to discover the port and open a matching
listener -- was tried and abandoned. Measured against the real device: a full scan of the
ephemeral range took 2.2-2.7s (own scanner, `nmap -sT`, and a real root/`CAP_NET_RAW`
`nmap -sS` SYN scan all agreed), well past the client's ~2s patience, because **the speaker
itself drops/filters scan traffic and forces retransmit timeouts** -- not scanner overhead.
Narrowing the range and threading the scan cut this to ~1.4s, but a later live pairing
attempt used a port outside the narrowed range, and separately the relay's scan sent three
probes to a port the client was actively trying to reach without ever getting a response.
The speaker doesn't reliably answer probes under load, independent of range or speed, so no
amount of scanning cleverness makes discovery reliable.

**The actual fix (`relay/app.py::ensure_airplay_event_ports`): don't scan or guess at all.**
`PortRelay` only ever opens its own connection to the speaker *at the moment a real client
connects* to it, so the relay can just permanently listen on the entire candidate range
from startup. Whichever port the client's pair-verify payload actually names, a listener is
already there — the relay never sends the speaker a single probe packet, so there's no race
and no discovery-reliability problem to solve. One-time cost: a few seconds to bind tens of
thousands of local sockets at startup, and a raised file-descriptor limit
(`docker-compose.yml`'s `ulimits.nofile`, since the container's default of 1024 isn't
enough for that many listening sockets).

**That range choice broke the whole host once, and is now deliberately narrower.** The
first version listened on the full `32768-65535`. With `network_mode: host`, those listening
sockets aren't sandboxed to the container -- they compete with the kernel's own pool of
*source* ports for any outbound connection the host makes, or any other container's NAT'd
traffic (Docker's SNAT for bridge-mode containers, e.g. `bose-app`, still routes through the
host). `32768-65535` is also Linux's default `net.ipv4.ip_local_port_range`
(`32768-60999` on the relay host), so binding nearly all of it starved the entire host of
outbound ports: `curl` on the host itself failed with `Cannot assign requested address`,
and `bose-app`'s web UI broke (page loads reused already-established connections; static
assets needing a fresh one 404'd). Fixed by narrowing `AIRPLAY_EVENT_PORT_RANGE` to
`32768-61000` and reserving `61001-65535` for the host's own ephemeral range via
`sysctl` (`net.ipv4.ip_local_port_range`, see README.md) -- the two ranges must never
overlap again.

### (superseded) Original writeup

The relay (`bose_soundtouch_relay/`) was built, deployed to the relay host, and confirmed
working for the SoundTouch API, gabbo push feed, and Spotify Connect — all relayed correctly
end-to-end from a real VLAN1 client. AirPlay was extensively chased separately and is
conclusively **not fixable by this project**:

- Real iOS clients' AirPlay 2 pairing handshake (`GET /info` → `pair-verify` → `pair-setup`)
  stalls at the identical byte-for-byte point on every attempt, whether going through the
  relay or connecting directly to the speaker, bypassing the relay entirely. Confirmed via
  repeated `tcpdump`/`tshark` packet captures.
- Testing with `pyatv` (a proper Python AirPlay/RAOP client library, much better diagnostics
  than raw packet capture) surfaced two distinct issues:
  1. AirPlay 2 negotiates an additional dynamically-assigned "event channel" port during
     setup that this relay doesn't proxy (only the fixed control port) — a real, buildable
     gap in the relay's scope.
  2. But connecting with **zero relay involved at all**, `pyatv` got further than any iOS
     attempt (past pairing, into real RTSP session/metadata setup) and then hit
     `ConnectionLostError` — proving the speaker's own AirPlay 2 firmware drops the
     connection, independent of any relay.
- The speaker's firmware (`27.0.6.46330.5043500`, dated Nov 2019) is end-of-life and cannot
  be updated, so this cannot be resolved on the device side either.

~~**Decision: do not build the dynamic-port AirPlay relaying.**~~ Reversed — see above.

## Superseded: "AirPlay playback fails inside the speaker's firmware" — the relay was missing the PTP clock-sync channel

With the standby-listener fix (and its port-range correction) deployed, live testing against
the real speaker and a real iPhone on VLAN1 settled this conclusively:

- A live capture showed a real, sustained 5.4MB audio stream flow phone → relay → speaker,
  cleanly ACKed the entire way with zero drops -- proof the mDNS impersonation, TCP relay,
  and dynamic-port coverage all work correctly, under real load, not just at handshake time.
- Polling the speaker's own SoundTouch API (`/now_playing`) live during two independent
  connection attempts showed an identical, reproducible sequence both times: the speaker
  correctly recognizes the session (`source="AIRPLAY"`, `playStatus="BUFFERING_STATE"`
  within ~0.3s of the control connection), then never advances past buffering to actual
  playback, and the session times out and reverts (`source="INVALID_SOURCE"`) on its own
  after ~10 seconds. No audio was ever heard.

This is a strictly more precise diagnosis than the original `pyatv`-based one above: that
test's "proves the speaker drops the connection" conclusion was itself flawed (see the
"Superseded" section), but its underlying instinct -- that the speaker's own AirPlay 2
implementation is the actual dead end -- turns out to be correct, just for a different and
now precisely located reason: not pairing, not the dynamic port, but the buffer-to-play
transition inside the speaker's own decode pipeline. Since the firmware
(`27.0.6.46330.5043500`, Nov 2019) is end-of-life with no update path, this cannot be
resolved from the relay side, and there is no further relay-side work that would change the
outcome -- every layer under this project's control is now proven correct.

~~**AirPlay is out of scope for this project going forward.**~~ Reversed — see below. The
"buffer-to-play transition fails inside the firmware" localization was accurate as an
observation, but the *cause* wasn't firmware at all.

## Current: the buffering stall is a relay gap — PTP clock sync over UDP was never relayed

The tell was sitting in the speaker's own `_airplay._tcp` TXT record all along:
`features=0x445F8A00,0x1C340`. Decoded as the standard 64-bit AirPlay feature mask, bits
**40 (SupportsBufferedAudio)** and **41 (SupportsPTP)** are set. In AirPlay 2 buffered
mode the audio itself rides over TCP — which is exactly why the 5.4MB stream above flowed
perfectly — but the receiver gates *playback* on PTP clock synchronization with the
sender, carried over **UDP ports 319/320**. The relay forwarded TCP only. The phone's PTP
datagrams went to the relay's advertised address, hit a closed UDP port, the speaker's
clock never synced, and it sat in `BUFFERING_STATE` until the session gave up (~10s) —
precisely the observed `AIRPLAY/BUFFERING_STATE → INVALID_SOURCE` sequence, with zero
audio, on every attempt.

So each layer of the earlier diagnosis was right about what it saw and wrong about what
it implied: the stream really did flow (TCP was fine), the speaker really did stall in
its buffer-to-play transition (it was waiting for a clock), and no amount of TCP-side
relay work could ever have changed the outcome.

**Fix (`relay/udp.py::UdpPortRelay`): the relay now forwards UDP too** — a single-socket
two-peer relay per port that shuttles datagrams between the speaker (known by address)
and the most-recent non-speaker peer, from the same bound socket so forwarded datagrams
keep their expected source port. The side symptom where the `bose_st300` UI "went crazy"
(paused → unknown) during AirPlay attempts was just the UI honestly rendering the
`BUFFERING_STATE`/`INVALID_SOURCE` churn and needs no fix of its own.

**What the first live test with UDP relaying actually showed (playback WORKS now — and
refines the theory):** the speaker went `STANDBY → AIRPLAY/BUFFERING_STATE →
AIRPLAY/PLAY_STATE` in ~3s and real audio played — the first time it ever crossed the
buffering wall. But a packet capture during that working session showed **no 319/320
traffic at all**. Audio and events rode two TCP connections (control 7000 + a standby
event port), while the phone sent steady UDP (~14 pkt/s + a ~1/s trickle — control and
timing) to two *dynamically-numbered* ports on the relay in the same 32768–61000 range
the TCP event channel uses — negotiated inside the encrypted SETUP, exactly like the
event port. Those datagrams initially hit closed ports and died silently (playback
tolerated it, single-room buffered mode being receiver-clocked, but the client retried
into the void the whole session — a plausible source of the earlier stalls'
flakiness). So the standby-listener approach now covers **UDP as well as TCP** across
32768–61000 (`relay/app.py::ensure_airplay_event_ports`), and 319/320 stay bound via
`ensure_airplay_ptp_ports` as cheap insurance for senders that do use classic PTP.
