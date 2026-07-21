# bose-soundtouch-relay

A single-NIC relay that makes a Bose SoundTouch 300 reachable from other VLANs.

## Why

The speaker's embedded network stack only replies to peers it considers on-link, so any
direct TCP connection from another VLAN (AirPlay, Spotify Connect re-pairing, the
SoundTouch API, the `bose_st300` web remote) fails even though mDNS discovery crosses the
VLAN boundary fine. See `FINDINGS.md` for the full diagnosis.

## How it works

Run this on any host on the **speaker's own VLAN/subnet** — no second interface needed.
Ordinary inter-VLAN unicast routing already works for well-behaved hosts (it's specifically
the speaker's stack that refuses off-subnet peers), so:

1. **TCP relay** (`relay/proxy.py`) — for every port a client connects to on the relay, opens
   a same-subnet connection to the real speaker and pipes bytes both ways. The relay is
   on-link with the speaker, so the speaker's cross-subnet bug never comes into play; other
   VLANs reach the relay itself over normal routing.
2. **mDNS impersonation** (`relay/mdns.py`) — browses the speaker's real service
   announcements and republishes each one, addressed at the relay instead of the speaker.
   The network's existing mDNS reflection (already confirmed working cross-VLAN) carries the
   republished record to other VLANs for free. Since the speaker's own (unusable)
   announcement is still on the wire too, the relay's copy gets a distinct instance name
   (` (relay)` suffix by default) so the two don't collide — clients will see both entries,
   but only the relay's one actually connects.

Everything — including the `bose_st300` web remote — should point at the relay's address,
not the speaker's real IP.

## Configuration

Edit `config.toml`, or set the equivalent env vars (env wins, useful for container deploys
without a rebuild):

| config.toml | env var | meaning |
|---|---|---|
| `speaker.host` | `SPEAKER_HOST` | the speaker's real IP on its own VLAN |
| `relay.host` | `RELAY_HOST` | this host's own IP, same VLAN/subnet as the speaker |
| `speaker.service_types` | — | mDNS service types to impersonate |
| `speaker.extra_ports` | — | ports to relay unconditionally (not mDNS-advertised on their own, e.g. the gabbo push feed on 8080) |
| `relay.name_suffix` | — | appended to each impersonated service's instance name |

## Commands

```bash
uv sync
uv run python -m unittest discover tests/ -v   # tests
uv run python main.py                            # run the relay (needs root/CAP_NET_BIND if binding low ports)
```

## Deployment

Ships as its own container with `network_mode: host` (see `docker-compose.yml`) — it needs
real multicast and to bind real ports directly on the LAN interface, neither of which work
through Docker's default bridge networking:

```bash
cp .env.example .env   # fill in SPEAKER_HOST / RELAY_HOST
docker compose up -d --build
```

**Required host-side change: narrow the host's own ephemeral port range first.** The
relay's standby AirPlay listeners (see "Status" below) permanently occupy TCP ports
`32768-61000` on the relay host's interface. Because of `network_mode: host`, those aren't
sandboxed to the container — with the kernel's default `net.ipv4.ip_local_port_range`
(`32768-60999` on most Linux distros), the relay competes with the *host itself* (and every
other container's outbound/NAT'd traffic) for source ports, and previously starved the
whole host of the ability to make any new outbound connection at all. Before starting the
relay, reserve a slice above `61000` for the host's own use:

```bash
echo 'net.ipv4.ip_local_port_range = 61001 65535' | sudo tee /etc/sysctl.d/99-bose-relay.conf
sudo sysctl --system
```

**Deliberately not behind `homelab-proxy`/nginx on port 80.** That proxy does plain HTTP
reverse-proxying by subpath (`http://host/<route>/` → `127.0.0.1:<port>`), which only works
for apps that themselves speak HTTP and tolerate a path prefix — like `bose_st300`. AirPlay,
Spotify Connect Zeroconf, and the SoundTouch API/gabbo feed aren't that: clients connect
directly to whatever `IP:port` mDNS advertises, not to an HTTP path, so routing this through
nginx wouldn't make any of them reachable.

## Known issues

- **AirPlay from the Spotify app does not work.** Streaming to the speaker over AirPlay
  works from other apps, and Spotify playback works via Spotify Connect (the speaker's
  native integration, relayed on port 8200) — but choosing the speaker as an AirPlay
  output from inside the Spotify app fails. Not yet diagnosed; use Spotify Connect for
  Spotify in the meantime.

## Status

**Working:** SoundTouch API (8090), gabbo push feed (8080), and Spotify Connect Zeroconf
(8200) are all confirmed working end-to-end through the relay — mDNS impersonation and TCP
relaying both verified correct, from real devices on another VLAN.

**Fixed: mDNS-discovered ports were never actually relayed.** A `NameError` in the
service-discovery callback (`_AIRPLAY_SERVICE_TYPES` was referenced but never defined in
`relay/app.py`) killed the callback for *every* discovered service, silently — the
impersonated mDNS records still got published, but no TCP listener was ever started for
the advertised ports, so clients saw the relay's announcement and then got
connection-refused. Only `extra_ports` (8080) escaped, being set up before discovery.
The callback dispatch is fixed, and failures in it are now logged loudly instead of
vanishing into an unawaited task.

**AirPlay: fixed, via permanent standby listeners for the dynamic event-channel port.** A
live packet capture of a real cross-VLAN pairing attempt (phone on VLAN1, relay + speaker
on VLAN2) showed the control channel (port 7000) completing a full pair-setup/pair-verify
exchange through the relay without issue, then the client opening a brand-new connection to
a second, dynamically numbered port and getting an immediate `RST` — nothing was listening
there. This overturns the earlier "speaker firmware is a dead end" conclusion in
`FINDINGS.md`: that conclusion came from a `pyatv` test run *on the speaker's own subnet*,
where the dynamic port is naturally reachable directly and this exact gap never gets
exercised. The real cross-VLAN path was never actually broken by the firmware — it was
broken by this relay not knowing about the second port.

The port number is embedded in the encrypted pair-verify payload, so the relay can never
read it off the wire — and trying to *discover* it by probing the speaker in advance
doesn't work either: measured against the real device, a full scan of the ephemeral range
took 2.2-2.7s (with our own scanner, `nmap -sT`, and even a real root/`CAP_NET_RAW`
`nmap -sS` SYN scan), well past the client's own ~2s patience, because the speaker itself
drops/filters scan traffic and forces retransmit timeouts. Narrowing the range and
threading the scan brought that down, but a live capture of a real pairing attempt then
used a port *outside* the narrowed range, and separately showed the relay's own scan
probing a port the client was actively trying to reach three times without ever getting a
response — the speaker just doesn't reliably answer probes under load, independent of
range or speed. No amount of scanning cleverness fixes an unreliable target.

So the relay doesn't scan or guess at all. Since it only ever opens its own connection to
the speaker *at the moment a real client connects* (see `relay/proxy.py::PortRelay`), it
can simply listen on the entire candidate range (`32768-61000`, ~28k ports) permanently
from startup (`relay/app.py::ensure_airplay_event_ports`). Whichever port the client's
pair-verify payload actually names, a listener is already there waiting — the relay never
sends the speaker a single probe packet, so there's no race and no extra load on an
already fragile embedded stack. Two one-time costs: binding ~28k local sockets at startup
(a few seconds), and a raised file-descriptor limit in `docker-compose.yml`
(`ulimits.nofile`, since the container's default of 1024 isn't enough).

**This range doubles as the host's own ephemeral port range, and that collision took the
whole host down once already.** With `network_mode: host`, the relay's standby listeners
aren't sandboxed to the container — they compete with the kernel's own pool of *source*
ports for outbound connections (used by the host directly, and by every other container's
NAT'd traffic, e.g. `bose-app`'s bridge-mode container still routes through the host's
SNAT). The first deployment of this range bound almost all of `32768-65535`, which is also
Linux's default `net.ipv4.ip_local_port_range` (`32768-60999`) — starving the entire host
of outbound ports and breaking unrelated services (`bose-app`'s UI went down: some requests
worked off already-established connections, others, needing a fresh one, failed outright).
Fixed by narrowing both sides so they never overlap — see "Deployment" above for the
required host-side `sysctl` change.

**AirPlay buffering stall: fixed by relaying PTP clock sync over UDP 319/320.** With the fixes
above deployed, a live capture showed the full pipeline working exactly as designed:
control channel pairing completes, the dynamic event-channel port is reachable via a
standby listener, and a real 5.4MB continuous audio stream flowed phone → relay → speaker,
cleanly ACKed the entire way with zero drops. Polling the speaker's own SoundTouch API
(`/now_playing`) live during two separate, independent connection attempts showed an
identical, reproducible pattern both times:

```
source="INVALID_SOURCE" playStatus="INVALID_PLAY_STATUS"    (connecting)
source="AIRPLAY"        playStatus="BUFFERING_STATE"         (~0.3s later)
source="INVALID_SOURCE"                                      (~10s later, gives up)
```

That stall was misdiagnosed as a firmware dead end for a while; the missing piece was
that the relay forwarded **TCP only, never UDP**. Besides the TCP event channel, the
encrypted SETUP exchange also names dynamically-numbered **UDP** ports for
timing/control, in the same 32768–61000 range — observed live: a real session sent
steady UDP to two such ports on the relay, dying silently against closed ports. The fix
(`relay/udp.py::UdpPortRelay`) extends the standby-listener approach to UDP across the
same range, plus UDP 319/320 for senders that use classic PTP clock sync (the speaker
advertises feature bits 40/41, buffered audio + PTP). First live test after the fix:
`STANDBY → AIRPLAY/BUFFERING_STATE → PLAY_STATE` in ~3s, with real audio — the first
time playback ever started. See `FINDINGS.md` for the full story.
