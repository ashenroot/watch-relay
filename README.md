# watch-relay

A small self-hosted relay that fetches things a browser cannot, and serves
them back as JSON.

It exists because of CORS. Every feed below answers an ordinary HTTP client
perfectly well and none of them sends an `Access-Control-Allow-Origin`
header, so a dashboard running in a browser is blocked before the request
leaves. A few are not HTTP at all. This process sits in between.

It backs two dashboards — [comms-watch](https://github.com/ashenroot/comms-watch) (amateur radio
situational awareness) and [grid-watch](https://github.com/ashenroot/grid-watch) (US bulk power
system) — and you can run either half or both. One process, one systemd
unit, one config file.

```bash
git clone https://github.com/ashenroot/watch-relay
cd watch-relay
sudo ./install.sh
```

Later, when a new version lands:

```bash
git pull && sudo ./upgrade.sh
```

---

## What it serves

| Route | What it is | Why it needs a relay |
|---|---|---|
| `/aprs.json` | APRS-IS stations near a grid square | Raw TCP on port 14580, not HTTP at all |
| `/netlogger.json` | Active amateur radio nets | No CORS header; also rate-limited to 1 call/min |
| `/repeaters.json` | Analog repeaters near a point | Upstream answers "near this point" only, so the relay sweeps and caches |
| `/kev.json` | CISA KEV split into OT and perimeter tiers, EPSS joined on | No CORS header; also saves the browser four extra round trips |
| `/doe202c.json` | DOE Federal Power Act 202(c) orders, parsed and classified | No CORS header; HTML needs parsing server-side |
| `/nerc.json` | NERC alerts and reliability assessments | Index pages are JavaScript-rendered and empty to a plain fetch |
| `/isoalerts.json` | ISO/RTO operational alerts, normalised across operators | Four different formats, one of them a zip of XML |
| `/digest.json` | Daily grid briefing digests (GET open, POST needs a token) | Needs somewhere durable to accumulate |
| `/briefmail.json` | Whether the mailbox reader is finding your brief | — |
| `/health` | Everything's status at once | — |

Set `GRIDWATCH_ENABLED=false` for the ham feeds only, or leave the callsign
blank at install time for the grid feeds only. Either half runs without the
other.

## Requirements

Debian or Ubuntu with systemd, Python 3.11+, and root. Two Python
dependencies, both pinned: `aprslib` and `aiohttp`. A domain name and port 80
reachable from the internet if you want `install.sh` to get you a Let's
Encrypt certificate; `--no-tls` skips that and leaves the relay on
`127.0.0.1` for you to put your own terminator in front of.

An amateur radio licence, if you want the APRS feed. The relay logs in to
APRS-IS with a passcode derived from your callsign and is **receive-only by
construction** — it never transmits.

## Design notes worth knowing before you trust the output

These are the rules the code actually follows. They are here because each
one was arrived at by getting it wrong first.

**A failed fetch is never rendered as a quiet result.** Every polled feed
carries its last payload *and*, separately, whether the last attempt
succeeded. An ISO with no reachable feed is reported as unavailable with the
reason, never as normal. `/isoalerts.json` includes a `reading_note` saying
how many operators its `worst_tier` does not cover.

**A severity tier is never reported without the cause that produced it.**
Eight operators use eight vocabularies, so the relay normalises them onto one
five-step scale — but every alert carries the operator's own wording and the
matched phrase alongside the number. Collapsing "defer discretionary
maintenance" and "shed firm load" into one integer is how a dashboard starts
lying.

**Unknown is not "no".** CISA's `knownRansomwareCampaignUse` reads Unknown
for most entries. It is passed through verbatim.

**Scheduled is not in force.** A declaration starting tomorrow is reported
with `status: scheduled`, not as active. Timezone conversion uses `zoneinfo`,
not a fixed offset, because a hardcoded −7 is wrong from November.

**It is a polite client.** NetLogger's published one-call-per-minute limit is
a floor in code regardless of configuration — this relay is what stands
between however many browser tabs you open and NetLogger seeing one request
a minute. The repeater sweep runs fully serial with 12 seconds between calls
because the upstream is a free service that does rate-limit, and identifies
itself in its User-Agent.

## Configuration

Everything lives in `/opt/aprs-relay/relay.env`, created from
[`relay.env.example`](relay.env.example), which documents every setting.
`install.sh` writes it mode 600 and `upgrade.sh` keeps it that way — it holds
a mailbox password and two tokens.

`upgrade.sh` adds settings introduced by a new version with their defaults
and **never overwrites a value you have set**. `./upgrade.sh --dry-run` shows
you exactly which keys it would add before it adds them.

### Feeds that need credentials

Three operators publish alerts but not publicly:

- **PJM** — free non-member Data Miner 2 key; `relay.env.example` has the
  four-step procedure. Note the restriction PJM attaches: non-member data is
  for internal business use, and publishing or making derivatives of it
  requires membership. Hold your own key and decide for yourself what you may
  do with what it returns. That is why no key ships with this project.
- **ISO-NE** — free registered account.
- **MISO, SPP, Southern** — no public alert feed this relay can reach, as of
  the last check. They are reported as unavailable with the specific reason.

Without any of these, you still get CAISO, ERCOT and NYISO.

## What has been tested, and what has not

Being specific, because an installer you run as root deserves it.

**Exercised:** `bash -n` and `shellcheck -S warning` clean on all three
scripts; both `--dry-run` paths; a real end-to-end `install.sh` into a
sandbox root — apt, venv, dependency install, config generation, file
permissions; `systemd-analyze verify` on the generated unit; the relay
starting from the installed location and answering on all ten routes with
live upstream data; `upgrade.sh` detecting a code change, adding a missing
setting, and leaving existing values and tokens untouched.

**Not exercised:** `systemctl enable --now`, nginx loading its config, and
certbot. The test environment has no running init and no nginx. Those three
steps are lifted unchanged from a predecessor script that has been running in
production for months, but they have not been run by this repo's CI, and you
should read them before running as root. They are at the bottom of
`install.sh` and they are short.

If you are nervous, `--dry-run` prints every action without taking any, and
`--no-tls` skips nginx and certbot entirely.

## Licence

MIT. See [LICENSE](LICENSE).

## A note on the names

The service is `aprs-relay`, it installs to `/opt/aprs-relay`, and the Python
file is `comms_watch_relay.py` — names from when this only served amateur
radio feeds. They are deliberately unchanged: an existing install is
recognised by exactly those paths, and renaming them would orphan every relay
already deployed. The project outgrew its name. That is cheaper to explain
than to fix.
