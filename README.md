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

**Deliberately not behind `homelab-proxy`/nginx on port 80.** That proxy does plain HTTP
reverse-proxying by subpath (`http://host/<route>/` → `127.0.0.1:<port>`), which only works
for apps that themselves speak HTTP and tolerate a path prefix — like `bose_st300`. AirPlay,
Spotify Connect Zeroconf, and the SoundTouch API/gabbo feed aren't that: clients connect
directly to whatever `IP:port` mDNS advertises, not to an HTTP path, so routing this through
nginx wouldn't make any of them reachable.

## Status

**Working:** SoundTouch API (8090), gabbo push feed (8080), and Spotify Connect Zeroconf
(8200) are all confirmed working end-to-end through the relay — mDNS impersonation and TCP
relaying both verified correct, from real devices on another VLAN.

**AirPlay: not supported, and not fixable here.** Confirmed via extensive packet capture
and direct testing with `pyatv` (see FINDINGS.md):
1. The speaker's AirPlay 2 pairing handshake (`pair-setup`) never completes for a real iOS
   client, with or without the relay in the path — identical failure connecting directly to
   the speaker, bypassing the relay entirely.
2. Separately, AirPlay 2 also needs a dynamically-assigned second port (an "event channel"
   negotiated during setup) that this relay doesn't proxy — a real, buildable gap in the
   relay's scope.
3. But even with *zero relay involved*, `pyatv` connecting directly to the speaker still
   gets `ConnectionLostError` partway through real RTSP session setup — proving the failure
   is in the speaker's own (end-of-life, unpatchable) AirPlay 2 firmware, not the relay.

Building out the dynamic-port relaying (point 2) was deliberately not pursued, since point 3
means it wouldn't make AirPlay actually work regardless — the speaker itself doesn't
complete the connection even with no relay involved at all.
