# Pexip Infinity Classification Policy Server

A very simple [Pexip Infinity](https://www.pexip.com/) **External Policy
Server** that does two things:

1. **Classifies a virtual meeting room based on the caller's domain.**
   When Pexip routes a new call, this server inspects the caller's
   `remote_alias` (e.g. `sip:alice@example.com`), looks up the domain
   (`example.com`) in an administrator-managed table, and returns a
   classification level (an integer in the range **1–5**, where `1` is
   the lowest / most permissive level and `5` is the highest / most
   restrictive) for the meeting. As additional participants join, the
   meeting's classification is re-evaluated and set to the **lowest**
   level across every joined participant's domain — so admitting a
   less-trusted party declassifies the meeting to their level rather
   than silently raising it.
2. **Adds an elapsed-time conference timer to every meeting.**
   Because conference timers can only be set through the
   [Pexip Client API](https://docs.pexip.com/api_client/api_rest.htm),
   the policy server joins as a "Policy Server" participant via the
   Client API, calls `set_clock` with `type: "elapsed"` after the meeting
   starts, and then **stays in the meeting** (refreshing its token in the
   background) until the conference ends.

It also ships a tiny admin UX (a single HTML page) so an administrator
can manage `domain → classification level` mappings with no command-line
work.

The service is a Flask + SQLite app, kept intentionally small. It was
designed using the
[`sidumorjens/pexip-claude-skills`](https://github.com/sidumorjens/pexip-claude-skills)
reference repo (skills `pexip-external-policy`, `pexip-policy-server`
and `pexip-client-api`).

## Architecture at a glance

```
Pexip Infinity ──POST /policy/v1/service/configuration──▶ this server
Pexip Infinity ──POST /policy/v1/participant/properties─▶ this server
                                                               │
                                                  Client API ◀─┘
                                                  (request_token,
                                                   set_classification_level,
                                                   set_clock {type:"elapsed"},
                                                   refresh_token …,
                                                   release_token)

Administrator ──HTTP browser──▶ /                  (admin UX)
```

* **`service_configuration`** — extracts the caller domain, looks up
  the level, and returns a valid configuration for an on-the-fly VMR.
  The resolved level is stored in SQLite under the meeting's alias and
  becomes the meeting's initial classification.
* **`participant_properties`** — called by Pexip for every participant
  joining a meeting. On each call the server:
  1. Looks up the joining participant's domain in the mappings table.
  2. Recomputes the meeting's effective classification as the
     **lowest** level across every joined participant's domain. A
     less-trusted participant therefore **declassifies** the meeting
     down to their level; a more-trusted participant **never** raises
     it.
  3. The first time it's called for a given meeting, it spawns a
     daemon thread that uses the Pexip Client API to set the
     classification level, add the elapsed timer, and then keep the
     Policy Server bot in the meeting (via periodic `refresh_token`)
     until the conference ends. The cross-process atomic gate (a
     `UNIQUE` constraint in SQLite) prevents duplicate Policy Server
     participants when multiple workers are running.
  4. If a later participant lowers the meeting's effective level, the
     server re-uses the live Policy Server bot's token to call
     `set_classification_level` again — no second bot is spawned.
* **Admin UX** — `GET /` renders an HTML page listing all mappings and
  letting the administrator add, edit, or remove them. Classification
  levels are integers in the range **1–5**, where `1` is the lowest /
  most permissive level and `5` is the highest / most restrictive.
  Backed by a small JSON API at `/api/domains`.

## Installation

All installation, configuration and day-2 operations instructions live
in [`INSTALL.md`](INSTALL.md). It is a step-by-step guide for deploying
the policy server on Ubuntu Server (dedicated service user, systemd,
Nginx + TLS, firewall, backups, upgrades and troubleshooting).

The rest of this document describes **what** the server does and
**how** it behaves at runtime — refer to it alongside `INSTALL.md` when
you're configuring or operating the service.

## Configuration reference

All configuration is via environment variables. `INSTALL.md` shows how
to put these into a config file that systemd loads; the table below is
the authoritative reference for what each variable does.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PEXIP_NODE` | yes | _empty_ | Hostname of a Pexip Conferencing Node, e.g. `conf.example.com`. If empty, the Client API side-effects are skipped (the server still classifies, but no banner/timer is applied on Pexip). |
| `PEXIP_PS_DISPLAY_NAME` | no | `Policy Server` | Display name used by the bot participant. The `participant_properties` callback matches on this name to elevate the bot to host. |
| `PEXIP_VERIFY_TLS` | no | `true` | Verify TLS cert of the Pexip node. Only set to `false` for lab use with self-signed certs. |
| `PEXIP_HTTP_TIMEOUT` | no | `10` | Client API HTTP timeout (seconds). |
| `DEFAULT_CLASSIFICATION_LEVEL` | no | `1` | Level used when the caller's domain has no mapping. Must be in the range `1`–`5`. Defaults to `1` (the lowest level) so unknown callers always pull the meeting down to the most permissive classification. |
| `POLICY_DB_PATH` | no | `./policy.db` | SQLite database location. Put this outside the source tree on a long-lived host so upgrades never touch it (e.g. `/var/lib/pexip-policy/policy.db`). |
| `ENABLE_CLIENT_API` | no | `true` | Master switch for Client API calls. Leave `true` in production. |

> The admin UX has no built-in authentication. If the server is
> reachable from anywhere except `localhost`, terminate it behind a
> reverse proxy that adds authentication on `/` and `/api/domains` —
> `INSTALL.md` includes a worked Nginx + Basic Auth example.

## Endpoints

### Policy (Pexip → server)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/policy/v1/service/configuration` | Return a VMR config; classify by the first caller's domain. |
| `POST` | `/policy/v1/participant/properties` | Recompute the meeting's classification on every join (lowest-wins). Also triggers the Client-API actions on the first participant, and pushes a re-classification when a later join lowers the level. |

Both also accept `GET` for compatibility with Pexip deployments that
issue policy lookups as `GET` with query parameters.

### Admin (humans / scripts → server)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | HTML page to view/add/delete mappings. |
| `GET` | `/api/domains` | List mappings (JSON). |
| `POST` | `/api/domains` | Create or update a mapping. Body: `{"domain": "...", "classification_level": N, "label": "..."}`. `classification_level` must be an integer in the range `1`–`5`. |
| `DELETE` | `/api/domains/<domain>` | Remove a mapping. |
| `GET` | `/healthz` | Liveness probe. |

## Domain matching rules

* Classification levels are integers in the range **1–5**
  (`1` = lowest / most permissive, `5` = highest / most restrictive).
* Exact match wins (`mail.example.com` matches a row for
  `mail.example.com`).
* Otherwise, the longest parent-domain match wins
  (`mail.example.com` falls back to `example.com`).
* If nothing matches, `DEFAULT_CLASSIFICATION_LEVEL` is used.
* Domain matching is case-insensitive.
* For non-domain callers (e.g. PSTN `+15551234567`), no domain is
  extracted and the default level applies.
* When more than one participant has joined a meeting, the meeting's
  effective classification is the **lowest** level across every joined
  participant's domain. A less-trusted participant therefore lowers the
  meeting's classification to their level; a more-trusted participant
  never raises it.

## Pexip theme (classification banner)

The classification banner shown to participants is rendered by Pexip
Infinity from a **theme** that you upload via the Pexip Management Node
(**Services → Themes**). A Pexip theme is a ZIP bundle whose
configuration file is `themeconfig.json` (sometimes referred to as
`theme.json`). Pexip will only display a banner for a numeric level that
is defined in the active theme's `classification.levels` map — so the
theme **must** define exactly the same set of integer levels that this
policy server emits (**1**–**5**).

If a level returned by the policy server is missing from the theme, the
Client API call `set_classification_level` is rejected and no banner
appears on the conference.

### Required alignment with the policy server

| Theme field | Must match | Notes |
|---|---|---|
| `classification.levels` keys | The integer range **`"1"`–`"5"`** | Same range enforced by `storage.MIN_CLASSIFICATION_LEVEL` / `MAX_CLASSIFICATION_LEVEL` and validated by the admin API. |
| `classification.levels[N].text` | The label you show admins in `/` | The strings here are what end users see on the banner. They don't have to be identical to the `label` column in the mappings table, but keeping them aligned avoids confusion. |
| `classification.default` | `DEFAULT_CLASSIFICATION_LEVEL` (default `1`) | Pexip uses this until the policy server pushes a level via the Client API; aligning the two means an unknown-domain meeting looks the same before and after the bot acts. |

### Example `themeconfig.json`

A minimal, working example that covers all five levels this server uses
is shipped at [`examples/themeconfig.json`](examples/themeconfig.json):

```json
{
    "theme_version": 2,
    "classification": {
        "levels": {
            "1": {"text": "Official",           "color": "0xff000000", "bgcolor": "0xff7ed957"},
            "2": {"text": "Official Sensitive", "color": "0xff000000", "bgcolor": "0xffffde59"},
            "3": {"text": "Restricted",         "color": "0xffffffff", "bgcolor": "0xffff914d"},
            "4": {"text": "Confidential",       "color": "0xffffffff", "bgcolor": "0xffd62828"},
            "5": {"text": "Secret",             "color": "0xffffffff", "bgcolor": "0xff1d1d1d"}
        },
        "default": 1
    }
}
```

Notes on the format (see Pexip's
[Customizing conference images and voice prompts using themes](https://docs.pexip.com/admin/themes_file_requirements.htm)
for the full reference):

* `theme_version: 2` is required for any modern (non-legacy) theme.
* `classification.levels` keys are numeric strings (`"1"`, not `1`).
* `text` is the string painted onto the banner.
* `color` / `bgcolor` are optional ARGB hex values
  (`0xAARRGGBB`); omit them to use Pexip's defaults.
* `classification.default` must be a key that exists in `levels`.

### Uploading the theme

1. Zip the file (and any other theme assets) so that `themeconfig.json`
   sits at the **top level** of the archive — e.g.
   `zip classification-theme.zip themeconfig.json`.
2. In the Pexip Management Node, go to **Services → Themes → Add theme**,
   upload the ZIP, and give it a name (for example `Classification`).
3. Apply the theme to the relevant Virtual Meeting Rooms / Virtual
   Auditoriums / Call Routing Rules (or set it as the global default
   theme) so that conferences created via this policy server use it.

Once the theme is in place, the policy server's
`set_classification_level` calls will paint the matching banner on
every conference.

## Development

```bash
pip install -r requirements.txt pytest
pytest -q
```

The tests use `ENABLE_CLIENT_API=false` so they never hit the network.
A stub Client-API object is also injected to confirm that the Client
API helper is invoked exactly once per conference even when many
participants join.

## Reference

* External policy protocol — [`pexip-external-policy` skill](https://github.com/sidumorjens/pexip-claude-skills/tree/main/skills/pexip-external-policy)
* Production patterns — [`pexip-policy-server` skill](https://github.com/sidumorjens/pexip-claude-skills/tree/main/skills/pexip-policy-server)
* Client API (token, `set_classification_level`, `set_clock`) —
  [`pexip-client-api` skill](https://github.com/sidumorjens/pexip-claude-skills/tree/main/skills/pexip-client-api)
