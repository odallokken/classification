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
  The resolved level is stored in SQLite under the meeting's alias.
* **`participant_properties`** — first time it's called for a given
  meeting, it spawns a daemon thread that uses the Pexip Client API to
  set the classification level, add the elapsed timer, and then keep
  the Policy Server bot in the meeting (via periodic `refresh_token`)
  until the conference ends. The cross-process atomic gate (a `UNIQUE`
  constraint in SQLite) prevents duplicate Policy Server participants
  when multiple workers are running.
* **Admin UX** — `GET /` renders an HTML page listing all mappings and
  letting the administrator add, edit, or remove them. Backed by a
  small JSON API at `/api/domains`.

> **Deploying on Ubuntu Server?** See [`INSTALL.md`](INSTALL.md) for a
> step-by-step guide (system user, systemd, Nginx + TLS, firewall,
> backups, troubleshooting).

## Quick start

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

All configuration is via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PEXIP_NODE` | yes (for prod) | _empty_ | Hostname of a Pexip Conferencing Node, e.g. `conf.example.com`. If empty, the Client API side-effects are skipped (server still classifies). |
| `PEXIP_PS_DISPLAY_NAME` | no | `Policy Server` | Display name used by the bot participant. |
| `PEXIP_VERIFY_TLS` | no | `true` | Verify TLS cert of the Pexip node. |
| `PEXIP_HTTP_TIMEOUT` | no | `10` | Client API HTTP timeout (seconds). |
| `DEFAULT_CLASSIFICATION_LEVEL` | no | `1` | Level used when the caller's domain has no mapping. Must be in the range 1..5. |
| `POLICY_DB_PATH` | no | `./policy.db` | SQLite database location. |
| `ENABLE_CLIENT_API` | no | `true` | Set to `false` to disable Client API calls (useful for dev/test). |

### 3. Run

```bash
python app.py                           # development
gunicorn -w 4 -b 0.0.0.0:8080 app:app   # production
```

Open `http://localhost:8080/` to manage domain mappings.

### 4. Point Pexip at it

In the Pexip admin UI:

1. **Platform → External policy** → set the Service URL to
   `https://<this-server>:8080/policy/v1`.
   (Pexip appends `/service/configuration` and `/participant/properties`
   itself.)
2. Tick **Use external policy** on the relevant Conferencing Node /
   System location.
3. Make sure the Conferencing Node's classification scheme already
   contains the levels (`0`, `1`, `2`, …) you intend to map domains to,
   otherwise `set_classification_level` will be rejected.

## Endpoints

### Policy (Pexip → server)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/policy/v1/service/configuration` | Return a VMR config; classify by caller domain. |
| `POST` | `/policy/v1/participant/properties` | Passthrough; triggers the Client-API actions once per meeting. |

Both also accept `GET` for compatibility with Pexip deployments that
issue policy lookups as `GET` with query parameters.

### Admin (humans / scripts → server)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | HTML page to view/add/delete mappings. |
| `GET` | `/api/domains` | List mappings (JSON). |
| `POST` | `/api/domains` | Create or update a mapping. Body: `{"domain": "...", "classification_level": N, "label": "..."}` |
| `DELETE` | `/api/domains/<domain>` | Remove a mapping. |
| `GET` | `/healthz` | Liveness probe. |

## Domain matching rules

* Exact match wins (`mail.example.com` matches a row for
  `mail.example.com`).
* Otherwise, the longest parent-domain match wins
  (`mail.example.com` falls back to `example.com`).
* If nothing matches, `DEFAULT_CLASSIFICATION_LEVEL` is used.
* Domain matching is case-insensitive.
* For non-domain callers (e.g. PSTN `+15551234567`), no domain is
  extracted and the default level applies.

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
