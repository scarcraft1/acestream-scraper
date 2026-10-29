# RFC: one import pipeline, and roles for pluggable modules

Companion to the design-question issue "one import pipeline, and a path towards pluggable sources".
Read against `develop` at `997dfe7`. Code paths are relative to `backend/app/` unless stated otherwise.
All examples are made up (`example://`, `https://example.invalid/list.m3u`, placeholder codes).

## 0. What is actually being proposed

Only two things are concrete:

1. **Phase 1: a single import pipeline, with no change in behaviour** (section 9). Same channels,
   same pruning, same status texts, same UI, no schema change.
2. **A handful of small, independent PRs** (section 9.3), each useful on its own.

Everything else in this document is **direction**: a sketch of where the code could go if you want it
to. It is written down so that phase 1 can be judged against a possible future, not to ask for that
future. If you prefer to stop after phase 1, or to never take a plugin registry into this repo, that
is a perfectly good outcome; anything we need beyond it would stay in our fork, on top of `develop`.

Labels used below:
- **read at 997dfe7**: checked in the code at that commit, with `file:line`.
- **measured**: counted with `grep` at `997dfe7`, or observed in a test run (section 10).
- **prototype**: built and tested in a local prototype on top of `develop`; not proposed as code.
- Everything else is a proposal and does not exist yet.

---

## 1. Summary

1. The scraper turns references into channels along five separate write paths, each with its own
   table, service, task and page (read at 997dfe7).
2. There is no common interface. The core knows Telegram, saved searches, events and W3U catalogues
   by name (read at 997dfe7).
3. The design names **seven roles** with a fixed contract: sources, discoverers, resolvers,
   playback engines, outputs, guides, and notifications/commands. Separately there are
   **infrastructure services** (programs behind an address) and a support piece, the **recipe helper**.
4. Each concrete piece would be a **module** that declares its roles and contract version and
   registers the same way whether it ships in the image or comes from a package (entry points).
5. Everything that writes channels goes through **one import pipeline** with three modes
   (`snapshot`, `insert_only`, `manual`) and today's pruning rules, **changing no result**.
   This is phase 1.
6. Anything an automated discoverer finds goes to a **proposals queue** that a person approves.
7. Nodes and tunnels (IPFS, ZeroNet, Tor, WARP, Gluetun) stay behind an address. Any piece that
   creates containers would be a separate, optional project, never part of the scraper.

---

## 2. The problem

Facts, with the cost each one adds when something new is added.

### 2.1 Five import paths (read at 997dfe7)

| Path | Inserts at | Exclusions checked | Prunes | TV channels from EPG | Auto-match |
|---|---|---|---|---|---|
| `ScraperService._apply_scrape` | `scraper_service.py:221` | `scraper_service.py:199`, again in `m3u_service.py:236` | yes, `scraper_service.py:208-212` | yes, `m3u_service.py:223` | yes, `scraper_service.py:246-248` |
| `CatalogueService._save` (W3U) | `catalogue_service.py:194` | `catalogue_service.py:156` | no | no | no |
| `ingest_records` (Telegram) | `incremental_ingestion_service.py:18` | `incremental_ingestion_service.py:10` and `telegram/ingestion.py:12` | no | no | no |
| `apply_candidates` (search) | `search_import_service.py:51` | `search_import_service.py:77` | no | no | no |
| manual add | `acestreamchannel_service.py:27` | `import_exclusion_service.py:56` | no | no | no |

- Only the first path sets `is_online=None`, `stream_url`, `stream_headers`, `tvg_*` and TV
  assignment. The others pass different subsets of fields.
- **Only the first path and manual add upsert.** W3U, Telegram and searches insert only when the
  channel does not exist yet: "Existing rows retain operator names, assignments, activity, headers
  and other-source ownership. Refresh never deletes" (`catalogue_service.py:182-185`);
  `incremental_ingestion_service.py:16-17` (an existing row only gets `last_seen`, `:23-26`);
  `search_import_service.py:50`.
- `source_url` is the only link between a channel and its source. Upsert overwrites it only when
  the new value is not null (`repositories/channel_repository.py:392-393`). Since insert-only paths
  never touch existing rows, a channel present in two sources belongs to the last **list** (or
  manual add) that brought it, never to a catalogue, a message or a search (the consequence is
  deduced from the code, not observed).
- Pruning rules today, all in `_apply_scrape` (`scraper_service.py:208-209`, comment "All-excluded,
  empty, failed and partial snapshots never prune"): prune only if channels remain after removing
  excluded ones, `not custom_recipe`, and `status == 'OK'` exactly. ZeroNet's direct `.m3u`/`.m3u8`
  and `.json` shortcuts return "Extracted N channels…" as their status (`scrapers/zeronet.py:300`
  and `:314`), so those references never prune; other ZeroNet pages go through the base class and do.
- **Cost:** a new kind of source picks one of the five paths and inherits its semantics. In the
  prototype, a paste-site source had to reuse `_apply_scrape` from inside `ScraperService` to get
  pruning and exclusions.

### 2.2 The playback engine is chosen by an ID prefix (measured)

- `startswith('url:')` appears **10 times in 6 files**: `api/endpoints/player.py:137`,
  `services/catalogue_service.py:252`, `services/recording_capture.py:95`,
  `services/player_service.py:312,482,490,530`, `services/recording_coordinator.py:406,418`,
  `services/tuner_playback_service.py:122`.
- `tuner_service.py:36` uses another rule (`url:[0-9a-f]{40}` plus `stream_url`), and
  `channel_status_service.py:225` decides "is HTTP" from a non-null `stream_url`. Three criteria
  for the same question.
- Some `url:` IDs are not channels: event media IDs (`event_service.py:30`, sha256 instead of sha1).
- The engine URL is computed in 7 places with different precedence: `config_service.py:48`,
  `check_engine_config_service.py:35`, `engine_client.py:90`, `acestream_status_service.py:12-24`,
  `system_services_service.py:106-107`, `stats_service.py:46`, `channel_status_service.py:95`.
- Acexy is not a separate engine but the `use_acexy` flag inside `EngineClient`
  (`engine_client.py:101`), checked again in `tuner_playback_service.py:194`,
  `recording_coordinator.py:420` and `remote_players/service.py:204`.
- **Cost:** a new engine (for example outpace, #217) means revisiting at least those 12 places.

### 2.3 The resolver is looked up in several places (measured)

- `for_stream_url(` is called 8 times: `api/endpoints/live.py:124,148`,
  `services/http_stream_check.py:37`, `services/m3u_service.py:90`,
  `services/player_http_source.py:105`, `services/playlist_service.py:432`,
  `services/w3u_parser.py:130`, and `services/live_providers/__init__.py:59`. Plus
  `for_source_url` in `scrapers/base.py:369`.
- `/live/` also decides by `needs_manifest` (`hls_manifest.py:44`), repeated in
  `playlist_service.py:428`.
- The resolver list is a fixed tuple of four built-in providers (`live_providers/__init__.py:20`).
- The same module is a source (by host, `for_source_url`) and a resolver (by scheme). The source
  part enters through an `if` inside `BaseScraper.scrape` (`scrapers/base.py:368-374`).
- **Cost:** a new resolver is a core change today. In the prototype, moving three FAST providers to
  a separate package touched none of those call sites.

### 2.4 The core knows concrete pieces (read at 997dfe7)

- `ChannelRepository.remove_search_memberships` (`channel_repository.py:442`): deleting a channel
  requires knowing that saved searches exist.
- `TaskService` has hooks specific to saved searches (`task_service.py:249,278`;
  `backend/main.py:231-232`).
- The `url_scraping` job also refreshes W3U, events and searches (`tasks/url_scraping_task.py:47-60`).
  Refreshing one event source requeues the whole scrape (`event_source_service.py:248`).
- `notification_service.scraping_completed` (`:55-95`) and `manual_job_service._summary` (`:10`)
  read each piece's keys by name.
- The recipe subprocess, meant to know neither database nor network, imports Telegram code
  (`recipe_worker.py:106`).
- **Cost:** each of the last three source kinds (W3U, events, searches) added its own call in
  `url_scraping_task.py`, its own key in the notification and its own summary. A fourth would too.

### 2.5 Three guarded HTTP families and seven redirect loops (read at 997dfe7)

- Families: `utils/outbound_http.py` (`source_session` on aiohttp; `source_get`/`source_post_json`
  on requests), `utils/bounded_source.py` (bounded reads) and `utils/url_guard.py` (validation).
- Seven modules rewrite "follow redirects while validating": `scrapers/http.py:43-65`,
  `extraction_service.py:57-80`, `api/endpoints/live.py:67-80`, `http_stream_check.py:17-27`,
  `w3u_parser.py:189-250`, `epg_sources.py:185-200`, `live_providers/base.py:25-43`.
- **Cost:** each new piece picks a family and copies the loop; a guard fix lands in seven places.

### 2.6 Four configuration layers (read at 997dfe7)

1. Environment → `Settings` (pydantic-settings), with legacy aliases.
2. `runtime-config.json` (#278), overriding the environment for the 31 options in
   `config/runtime-options.json`. Changes require a restart, by design.
3. The `settings` table (key/value).
4. JSON inside `settings` (`playback_routing`, `check_engine_config`, `epg_matching`, …).

- `SCRAPER_TIMEOUT/RETRIES` and `ZERONET_TIMEOUT/RETRIES` are declared (`config/settings.py:94-97`)
  and never read: the factory hard-codes 10/3 and 20/5 (`scrapers/__init__.py:28-37`).
- **Cost:** a new piece has no obvious place for its options nor a way to render them.

### 2.7 Five shapes of "how did the last run go" (read at 997dfe7)

`scraped_urls.status/last_processed/error_count`; `event_schedule_sources.last_attempt/last_success/last_result`;
`saved_searches.last_*` + `run_state`; `telegram_sources.state/last_success/message`;
`w3u_catalogues.updated_at` plus the partial flag inside the snapshot.

`scraped_urls.status` is free text: `OK`, `Error: {str(e)}` (`scrapers/base.py:417`,
`scrapers/ipfs.py:82`, `scrapers/zeronet.py:298`) or "Extracted N channels…". The UI shows green
only for the exact `'OK'` (`frontend/src/pages/Scraper.tsx:70`). **Cost:** no common view of
"which source is failing".

### 2.8 Infrastructure handled three ways (read at 997dfe7)

- IPFS and ZeroNet by address (`IPFS_GATEWAY_URL`, `ZERONET_URL`).
- WARP by process (`warp-cli` through `subprocess`, `warp_service.py:38`).
- Tor is started by `entrypoint.sh` (`start_tor`, `:523`); in Python it appears only in
  `diagnostics_service.SERVICES` (`:14`). The status panel uses a different list,
  `SERVICE_NAMES` (`system_services_service.py:31`, without Tor), with one branch per service in
  `_evaluate` (`:246`).
- **Cost:** Gluetun, I2P or an external node means touching the list, `_evaluate` and the wizard.

### 2.9 Smaller duplications

- `scrapers/base.py:358`, `scrapers/ipfs.py:67` and `scrapers/zeronet.py:279` repeat the
  "list → M3U → HTML → bare IDs" tree; `extraction_service.fetch_recipe_sample` repeats the
  ipfs/zeronet/regular choice with `isinstance`.
- Playlists and XMLTV (`playlist_service.py`), HDHomeRun emulation (`tuner_service.py`,
  `api/endpoints/tuner.py`) and the web player each live on their own and know resolvers and the
  relay directly. A new output (Xtream, MediaMTX) is another hand-written router.

---

## 3. Glossary

| Term | Meaning | Not to be confused with |
|---|---|---|
| **Reference** | Text that says where to read from: `https://example.invalid/list.m3u`, `ipfs://…`, `example://channel-7`. Stored in `scraped_urls.url`. | "URL": not every reference is HTTP. |
| **Role** | A function with a fixed contract that a module can fulfil. There are seven. | Module: one module can fulfil several roles. |
| **Source** | Role. Turns a reference into a list of streams. Writes nothing. | The `scraped_urls` row, which here is a *registered reference*. |
| **Discoverer** | Role. Produces proposals (new references, recipes, mappings) for the queue. | AceStream search, which imports channels (an `insert_only` source). |
| **Resolver** | Role. At play time, turns a stored stream with its own scheme into a fresh playable link. | Engine: a resolver only returns a link. |
| **Playback engine** | Role. Opens a resolved stream and releases it: direct engine, Acexy, HTTP input. | The infrastructure service that *runs* the engine binary. |
| **Output** | Role. Serves channels, guide and streams to a client in its format (m3u/XMLTV, HDHomeRun, web player, Xtream). Read-only; streams go through the relay. | Notifications. |
| **Guide** | Role. Turns a reference into programme data (XMLTV). | Source: produces no streams. |
| **Notifications and commands** | Role. Sends notices (webhook, bot) and, optionally, receives a closed set of commands. | Remote players and media servers (open question 6). |
| **Infrastructure service** | A program outside the scraper process (IPFS node, ZeroNet node, WARP, Tor, Gluetun, Acexy, the engine binary). The scraper knows only its address and how to check it. A declaration, not a code contract. | The source module that reads `ipfs://`. |
| **Recipe helper** | Support piece (#216). Proposes an extraction recipe for a new page, or a repair. Its output is always a proposal. | The recipe itself (`scraped_urls.extraction_recipe`). |
| **Import pipeline** | The single core path that writes channels: exclusions, mode, pruning, TV channels from EPG, auto-match where it happens today. Runs in the caller's session. | Any of today's five paths. |
| **Proposals queue** | Table of what an automated piece proposes and a person decides, each row with a `kind`. | The maintenance FIFO in `TaskService`. |
| **Module** | Python unit implementing one or more roles, declaring which and against which contract version. | Package: what is installed and carries modules. |
| **Built-in module** | One that ships with the scraper and follows the same contract as external ones. No package can replace it. | — |

Sports events stay outside the roles: they produce events and event media, not channels or guide data.
Nothing here touches them.

---

## 4. Roles and contracts (direction)

Shown as Python for readability; the formal shape is section 5.

### 4.0 Rules common to all roles

A module receives an injected context and imports only from the contract, never from `app.*`:

```python
class ModuleContext(Protocol):
    http: GuardedHTTP            # the only way out to the network (outbound_http underneath)
    log: logging.Logger          # with a secrets filter
    options: Mapping[str, Any]   # declared options, already validated
    deadline: float              # time.monotonic() limit
    cancelled: threading.Event   # cooperative
```

`GuardedHTTP` wraps `outbound_http.source_session` and `bounded_source`: validates every hop, pins
DNS, caps bytes and time. It gives modules one family; migrating the core is a separate PR.

Options use the same shape as `config/runtime-options.json`, which `/setup` already renders:

```python
class OptionSpec(BaseModel):
    name: str                      # UPPER_CASE, module prefix: EXAMPLE_DOMAIN
    label: str
    kind: Literal['text', 'url', 'int', 'bool', 'choice', 'secret']
    default: Any = None
    scope: Literal['module', 'reference'] = 'module'
```

| A module may | A module may not |
|---|---|
| Use the network through `ctx.http`. | Open its own sockets or HTTP clients. |
| Return data and raise `ModuleError(public_message)`. | Write to the database: all writes go through the pipeline, the queue or the core. |
| Log through `ctx.log` without source data. | Log tokens, credentials, response bodies or local paths. |
| Read its options from `ctx.options`. | Read the environment, `Settings` or the `settings` table directly. |
| Use bounded CPU within its deadline. | Open engine sessions. Only playback engines do. |

This is **not isolation**: a module runs in the same process with the same permissions. It is
enforced by review, by the contract and by a conformance suite that inspects imports. An installed
package is trusted code, like any dependency.

**Failure.** The core wraps every call; an exception never escapes to the core or to another
module. **A deadline discards, it does not cut:** a Python thread cannot be killed and `cancelled`
is cooperative, so the core stops waiting, drops a late result and records `TimeoutError`.
Work that must really be cut goes to a subprocess, as `recipe_worker` already does (`timeout=15`
in `extraction_service.py:33`; `RLIMIT_AS` 1 GiB and `RLIMIT_CPU` 10 s in `recipe_worker.py:102-103`).

Today's scrapers store `Error: {str(e)}`. Phase 1 keeps that; reducing it to the exception type
would be a visible change and a separate PR.

### 4.1 Source

```python
NAME = 'example'                   # [a-z][a-z0-9_-]{0,63}
LABEL = 'Example'
SCHEMES = ('example://',)
MODE: Literal['snapshot', 'insert_only'] = 'snapshot'   # 'manual' is core-only
OPTIONS: tuple[OptionSpec, ...] = ()

def accepts(reference: str) -> bool: ...        # optional
def normalize(reference: str) -> str: ...       # optional; one spelling, dedupes the queue
async def fetch(reference: str, ctx: ModuleContext) -> FetchResult: ...

class StreamEntry(BaseModel):
    id: str                        # content-id, infohash, or 'url:<sha1>' computed by the core
    name: str
    kind: Literal['acestream', 'http']
    stream_url: str | None = None
    headers: dict[str, str] = {}
    tvg_id: str | None = None
    group: str | None = None
    logo: str | None = None

class FetchResult(BaseModel):
    entries: list[StreamEntry]
    complete: bool = True          # False = partial read
    note: str = ''
```

**Three modes, the ones that already exist:**

| Mode | Used by | New channel | Existing channel | Prunes |
|---|---|---|---|---|
| `snapshot` | URL lists (HTTP, IPFS, ZeroNet, recipe) | insert | upsert (`source_url` only if not null) | with the three rules below |
| `insert_only` | W3U, Telegram, saved searches | insert | untouched (Telegram sets `last_seen`) | never |
| `manual` | manual add | insert | upsert, with `allow_excluded`, never propagated | never |

These match the project's own rules: saved searches "import additively" (`CLAUDE.md:758`),
W3U "refresh never deletes" (`catalogue_service.py:183`) and "Preserve existing channel metadata
and all channels on partial refresh" (`CLAUDE.md:612-613`).

**Pruning, exactly as today:** only in `snapshot`, only when (1) channels remain after exclusions,
(2) there is no custom recipe, and (3) `status == 'OK'` exactly. The pipeline keeps all three,
including the ZeroNet shortcut behaviour. Changing it would change results and belongs in its own PR.

A partial read (`complete=False`) writes what arrived and does not prune, because any status other
than `'OK'` already blocks pruning. New status strings such as "OK: empty" or "Partial: …" are
deferred: `Scraper.tsx:70` would render them grey, so they need their own visible change.

`HTTPScraper`, `IpfsScraper` and `ZeronetScraper` would become built-in sources claiming
`http(s)://`, `ipfs://`/`ipns://` and `zero://`. The extraction recipe stays data on the reference
and keeps winning over the module, as today (`scraper_service.py:66-77`).

### 4.2 Discoverer

```python
NAME = 'example-wiki'
LABEL = 'Codes listed on an example wiki'
INTERVAL_SECONDS = 86_400
async def discover(ctx: ModuleContext) -> list[Proposal]: ...

class Proposal(BaseModel):
    kind: ProposalKind = 'source'
    reference: str                 # normalised by the core with the owning source
    label: str = ''
    provenance: str = ''           # no tokens
```

- Returns its full list or nothing. Never downloads what it proposes nor opens engine sessions.
- Core caps: 200 proposals per call, 500 pending in total (prototype).
- Example: a discoverer that reads a wiki listing paste codes and proposes each code as a reference.
- Telegram could expose the same account as two separately enabled capabilities: an `insert_only`
  source (today's ID ingestion) and a discoverer of list links (off by default).
- Today there are no discoverers. `find_m3u_links` and W3U child traversal find references but read
  them immediately; they stay as they are.

### 4.3 Resolver

The current provider contract (`SCHEME`, `HOSTS`, `PLAY_HEADERS`, `channels()`, `resolve(ident)`,
optional `SERVE_MANIFEST`, `resolution_limits`) stays as is. Two changes:

- The "source by host" part (`HOSTS`, `channels()`) is declared as the source role of the same
  module, so the `if` in `scrapers/base.py:368-374` goes away.
- All lookups in 2.3 call one registry function. `for_stream_url` already exists; only the list it
  reads would come from the registry. Can go first as a small PR.

### 4.4 Playback engine

```python
class PlaybackEngine(Protocol):
    NAME: str                                    # 'direct', 'acexy', 'http'
    def handles(self, stream: StreamRef) -> bool: ...
    def open(self, stream: StreamRef, *, deadline: float) -> OpenedStream: ...
    def release(self, opened: OpenedStream, *, linger_seconds: float = 0) -> None: ...

def engine_for(stream: StreamRef, routing: PlaybackRouting) -> PlaybackEngine: ...   # one place
```

- `StreamRef.kind` comes from a `stream_kind(content_id, stream_url)` helper, no migration,
  replacing the ten `startswith('url:')` checks and the two other criteria. A small PR that can go first.
- **Not moved into the contract:** session ownership, deferred STOP, the single probe turn, the
  relay and tuner slots. They are project rules ("Direct playback uses process-wide source ownership
  leases… release exactly once", "Never fall back to playback when a configured checker fails",
  `CLAUDE.md:301-306`) and stay in the core. The checker engine stays direct.
- Errors stay `EngineUnavailableError` and `EngineRefusedError` (`engine_client.py:35,39`).

### 4.5 Output

A read-only router over `tv_channels` and the guide; streams always through the existing relay.

```python
NAME = 'xtream'
OPTIONS = (OptionSpec(name='XTREAM_PASSWORD', label='Password', kind='secret'),)
def router(ctx: OutputContext) -> APIRouter: ...   # mounted under its own prefix
```

- No output opens engine sessions on its own.
- An output that needs its own port (HDHomeRun's UDP 65001) or a binary (MediaMTX) is not a
  Python-only module; see 6.2.
- Exposed on the local network only, with its own credential.

### 4.6 Guide

A separate role because it produces timed programmes tied to a guide channel identity, has its own
refresh and retention cycle, and its failure never changes channel availability. `EPGService`
stays the public interface; a guide module only delivers XMLTV.

```python
async def fetch(reference: str, ctx: ModuleContext) -> GuideResult: ...

class GuideResult(BaseModel):
    xmltv_path: Path               # temp file supplied by the core, size-capped
    complete: bool = True
```

Today `epg_sources.py:205-208` reads the whole response and decompresses it in memory. A cap (for
example 64 MiB after decompression, configurable) would turn an oversized guide into a partial read.
A "page with recipe" guide could join #233 and #216: the recipe helper proposes how to extract a
non-XMLTV guide page, a person approves it.

### 4.7 Notifications and commands

```python
EVENTS = ('source.failed', 'proposal.created', 'module.rejected', 'system.started')
async def notify(notice: Notice, ctx: ModuleContext) -> None: ...
async def listen(ctx: ModuleContext, submit: Callable[[Command], Awaitable[CommandResult]]) -> None: ...  # optional

Command = AddReference | Rescan | Approve | Reject     # closed set, defined by the core
```

- `Notice` has generic fields (`event`, `title`, `counts`, `link`), so the notification stops
  reading each piece's keys by name (2.4).
- The module translates; the core authorises and executes. A command that changes a source counts
  as approval only from an allowed user with an explicit yes/no confirmation.
- Today's webhook (#228) would become the built-in `webhook` module.

### 4.8 Recipe helper

`async def propose(sample: RecipeSample, ctx) -> RecipeProposal | None`. `RecipeSample` is what
`/recipes/sample` already returns. Output always goes to the queue; it never writes
`extraction_recipe`. A proposed recipe is re-tested in the existing `recipe_worker` subprocess
before it is shown. Works without any model: with none available, requests simply wait.

---

## 5. Contract and versioning (direction)

The contract would be a small versioned artefact:

1. **Specification:** one `Protocol` per role and the exchanged models, each with a published JSON
   schema; version `major.minor`.
2. **Conformance suite:** tests an author runs against a module without the scraper (declaration,
   types, schemas, deadlines, imports only from the contract).
3. **Per-module declaration:** `ROLES = ('source', 'discoverer')`, `CONTRACT = (1, 0)`.
4. **Check on load:** the registry rejects what does not comply, without calling `fetch` or using
   the network.

Minor versions add optional things (checked by version, not `hasattr`); major versions are
incompatible and go in the changelog. A module written against a newer minor than the core's is
rejected at load time instead of failing at run time. In the prototype, packages imported
`app.services.live_providers.base` and `app.utils.outbound_http` directly; a helper added later
broke older scrapers at run time. That is the problem this solves.

---

## 6. Registry and lifecycle (direction)

### 6.1 Loading (prototype)

- One entry point group per role: `acestream_scraper.sources`, `.discoverers`, `.resolvers`,
  `.engines`, `.outputs`, `.guides`, `.notifiers`. The value is a module, not `module:attribute`.
- Built-ins register first; packages in entry point name order.
- The installed list is read from metadata without importing; only enabled modules are imported.
- Rejected **with a warning, never an exception**: import failure (including `SystemExit`),
  incomplete contract, duplicate name, scheme that prefixes another, host clash by suffix
  (`www.example.invalid` vs `example.invalid`), reserved core scheme, incompatible contract version.
- A package never replaces a built-in; if it tries, it is listed as `ignored`.
- With no module for a role, the core keeps working (no discoverers: no discovery job is registered).

States: `loaded`, `disabled` (a person turned it off), `rejected` (with reason), `ignored`.

### 6.2 Two classes of module

| Class | Examples | How it arrives |
|---|---|---|
| **Python-only** | a paste-site source, RSS, FAST providers, a chat bot | a pip-installable package in the image or a mounted path |
| **Needs more than Python** | speech models, MediaMTX, TVHeadend, native libraries | an image layer built on top of the official image |

Python does not unload modules. Enabling a module that is already installed can be done without a
restart (it is imported for the first time); disabling just stops calling it. Installing, updating
or removing code always needs a restart. If enabled modules were stored in `runtime-config.json`,
they would belong in a section next to `runtime`, not inside it, and `update_config`
(`backend/runtime_config.py:174-183`, which today rewrites only `version`, `setup_complete` and
`runtime`) would need to keep unknown sections. Whether any of this belongs in that file at all is
your call; the existing restart semantics of `/setup` would not change.

### 6.3 Optional hardening, only if modules exist at all

- A load-time budget per module that **measures** (an import cannot be interrupted) and marks slow
  modules for the next start.
- Safe start: before importing a newly enabled module, write "starting with X" to a small file of
  its own; clear it after the health check passes. Two crashed starts with the mark skip X. This
  turns a module that kills the process into two restarts, not a restart loop.
- Import inside the existing background start-up task (`asyncio.to_thread(initialize_database)`,
  `backend/main.py:223`), not in the event loop.

### 6.4 In the UI

Following the existing page pattern (`PageHeader` → `StatusLine` → `ContentSection`s): a
**Modules** page under System (name, roles, package, version, contract, state and reason; enable or
disable; options rendered from `OPTIONS`), the add-source dialog type selector built from the
registry instead of the fixed list (`Scraper.tsx:455-463`), and a `?tab=proposals` tab in Scraper.
API: `GET /api/v1/modules`, `/api/v1/proposals…`, with Pydantic DTOs in `openapi.json`.

---

## 7. Infrastructure services (direction)

```python
class ServiceSpec(BaseModel):
    name: str                      # 'ipfs', 'zeronet', 'tor', 'warp', 'gluetun', 'acexy', ...
    label: str
    kind: Literal['http_gateway', 'socks_proxy', 'engine_api', 'port_file']
    address_option: str            # 'IPFS_GATEWAY_URL', 'ACESTREAM_GLUETUN_PORT_FILE', ...
    health: HealthProbe | None
    placement: Literal['image', 'container', 'remote']
```

- `system_services_service` and `diagnostics_service` would read one declared list instead of two
  hard-coded ones (2.8). Tor and Gluetun (`port_file`) become visible without new branches.
  `/system/services` should return the same as today for the existing six.
- Services inside the image keep being started by the `entrypoint.sh` supervisor.
- WARP keeps its own `/warp/*` API.

**A container manager, far away and optional.** Creating or removing containers (Gluetun, a
separate IPFS node, MediaMTX) would never be the scraper's job: the scraper has no Docker socket and
should keep it that way. If anyone wanted it, it would be a separate project talking to Portainer or
Dokploy through a scoped API key (or a filtered socket proxy, opt-in), local network only, with an
audit log and an allow-list of labelled containers and digest-pinned images. It would **not**
replace or host `/setup`, and it is not part of this proposal.

---

## 8. Approvals and secrets

### 8.1 What runs on its own and what needs a person

Rule: **anything that changes a source is approved by a person.**

| Action | Automatic | Needs a person |
|---|---|---|
| Read a registered reference and update its channels | yes | — |
| Prune a `snapshot` reference after a complete read | yes | — |
| Check channels with the checker engine, within today's queue rules | yes | — |
| Propose a reference, recipe or mapping | yes, to the queue | — |
| Register a reference, enable a paused one, apply a recipe, disable or delete a source | — | yes |
| Enable, disable, install or remove a module | — | yes |

### 8.2 Proposals queue

Prototype: a table with `pending/approved/rejected`, unique normalised `reference`, a final decision
(409 on a second one); approving creates the source **paused** through
`ScraperService.create_scraped_url`; already queued or already registered references are never
re-proposed.

Direction: a `proposals` table with `kind` (`source`, `catalogue`, `recipe`, `channel_mapping`),
`target_id`, `payload`, `decided_by`; pending rows expire after 30 days; rejected and expired rows
are hidden but can be filtered and restored; a hard delete allows re-proposing. Approving a
`catalogue` opens the normal W3U preview rather than saving anything. The first step would be
`kind='source'` only.

An optional per-discoverer "trusted" switch (off by default) could skip the queue; whether that
counts as prior approval is open question 1.

### 8.3 Secrets

Today secrets are plain text in the database, documented as a deliberate constraint ("Real
constraints, not TODOs": remote-player passwords, Jellyfin keys and Plex tokens, never logged and
never returned by the API, `CLAUDE.md:128-143`), and `/setup` keeps credentials in deployment
configuration (`CLAUDE.md:739-740`). This design does not change that. Module options of kind
`secret` would follow the same rules: never logged, never returned by the API, shown as
"configured". An encrypted store like the one Telegram already uses (`TELEGRAM_SESSION_DIR`,
`TELEGRAM_KEY_FILE`) is only an idea, and only if you ever want it.

### 8.4 Content limit

No module of any role decodes or bypasses third-party protection (encryption, DRM, access control).
The registry cannot tell; it is a review rule.

---

## 9. Phases and small PRs

### 9.1 Mapping today → design

| Piece today | Main files | Role | Change | Phase |
|---|---|---|---|---|
| Five import paths | `_apply_scrape` and the other four | core | one pipeline, three modes, no result changes | 1 |
| HTTP list, IPFS, ZeroNet | `scrapers/*.py` | built-in sources | registry; shared loop stays in the base | 2 |
| W3U catalogues | `catalogue_service.py`, `w3u_parser.py` | built-in `insert_only` source | pipeline (1), registry (2); preview flow stays | 1-2 |
| Telegram account | `services/telegram/*` | source + optional discoverer | pipeline for ingestion; `Manager` stays long-lived | 1, 3 |
| Saved searches | `saved_search_service.py`, `search_import_service.py` | `insert_only` source | pipeline; FIFO reservations stay | 1 |
| Manual add | `acestreamchannel_service.py` | core entry | pipeline in `manual` mode | 1 |
| Live providers | `live_providers/*` | resolver + source | registry | 2 |
| Direct engine, Acexy, HTTP input | `engine_client.py`, `player_http_source.py` | playback engines | `stream_kind` (small PR), `engine_for` (5) | 5 |
| Playlists/XMLTV, HDHomeRun, web player | `playlist_service.py`, `tuner_service.py`, `player_service.py` | built-in outputs | registry | 7 |
| XMLTV guides | `epg_sources.py` | built-in guide | registry; `EPGService` unchanged | 6 |
| Webhook | `notification_service.py` | built-in notifier | `Notice` | 3 |
| Service status | `system_services_service.py`, `diagnostics_service.py` | `ServiceSpec` list | one declared list | 4 |
| Sports events | `event_source_service.py`, … | outside the roles | nothing | — |

Not rewritten: the `BaseScraper` extractors, the four resolvers, `EngineClient`, `PlayerHTTPSource`,
the relay, the player, recordings, `EPGService`, the Telegram `Manager`, the FIFO, the supervisor,
existing migrations.

### 9.2 Phase 1: one import pipeline (the concrete proposal)

- **What:** `ImportPipeline.apply(db, reference, entries, mode, *, policy=None)` in the core, with
  the three modes and the three pruning rules unchanged. The five paths call it. No plugins, no
  registry, no dependency on anything later.
- **The session belongs to the caller.** `apply` receives the path's session and does not commit;
  the caller commits, as today. The exclusion snapshot is taken with `snapshot(claim=True)` inside
  that transaction, or passed in `policy` if the path already took it. For Telegram, cursor and
  observations still commit with the import: "Source cursor and message observations commit with
  imports; never call URL snapshot deletion from a message consumer" (`CLAUDE.md:677-679`).
- **Out of phase 1:** events; `Notice`; `FetchResult` as a contract; new status strings; reducing
  `Error: {str(e)}` to the exception type.
- **Optional second PR:** W3U and saved searches move out of `url_scraping` into their own jobs on
  the same FIFO. Events stay where they are.
- **Tests:** existing tests per path unchanged; tests pinning today's behaviour written **before**
  anything moves; regression for the pruning rules (empty, recipe, status other than `'OK'`,
  including the ZeroNet `.m3u` shortcut); Telegram failing after import does not advance the cursor.
- **Rollback:** revert. No schema change.

Behaviour per path today, and with the pipeline (read at 997dfe7):

| Path | Session and commit | Exclusions | Writes | Prunes | Status written | With the pipeline |
|---|---|---|---|---|---|---|
| Lists (`_apply_scrape`) | own thread session, `with Session(bind=bind)`; one commit (`scraper_service.py:195,243`) | `snapshot(claim=True)` (`:199`) | upsert (`:221`) | three rules (`:208-212`) | `scraped_urls.status`, by the scraper, **before** import, in another session (`scrapers/base.py:432`); with a recipe, `scraper_service.py:87` | `apply(mode='snapshot')` in the same session; status written where it is today |
| W3U (`CatalogueService._save`) | `self.db` | `snapshot(claim=True)` (`catalogue_service.py:156`) | only if missing (`:184-185`) | never | `w3u_catalogues` snapshot with partial flag and `updated_at` (`:161-165`) | `apply(mode='insert_only')`; snapshot stays in `CatalogueService` |
| Telegram (`telegram/ingestion.py`) | own `SessionLocal()`; one commit with cursor and observations (`:11,38`) | `snapshot(claim=True)` (`:12`), passed to `ingest_records` | only if missing; existing rows get `last_seen` (`incremental_ingestion_service.py:16-26`) | never | `telegram_sources.state/last_success/message` (`ingestion.py:28`), same transaction | `apply(mode='insert_only', policy=…)`; same transaction |
| Searches (`apply_candidates`) | `self.db` | unclaimed for the network step (`search_import_service.py:69`), claimed before writing (`:77`) | only if missing (`:50-52`) | never | `saved_searches.last_result/...` (`saved_search_service.py:228-231`) | `apply(mode='insert_only', policy=…)` after the claim at `:77` |
| Manual add | request session; repository commits | `require_allowed` with `allow_excluded` (`api/endpoints/channels.py:205-206`) | upsert (`acestreamchannel_service.py:27`) | never | none | `apply(mode='manual')`; `allow_excluded` never propagates |

### 9.3 Small PRs, any time, independent

| PR | What | Schema |
|---|---|---|
| `stream_kind` | one helper instead of the 10 `startswith('url:')`, the regex in `tuner_service.py:36` and the criterion in `channel_status_service.py:225` | none |
| Engine URL | one function for "the engine URL" (7 places today, 2.2) | none |
| Dead settings | `SCRAPER_TIMEOUT`, `SCRAPER_RETRIES`, `ZERONET_TIMEOUT`, `ZERONET_RETRIES`: wire them up or remove them | none |
| Provider lookup | one function for the 8 `for_stream_url` calls (2.3) | none |
| Docs | `docs/dev/backend.md` is out of date | none |
| HTTP clients | unify the three families and seven loops (2.5) | none; later |

Rules for every PR: against `develop`; full suite with no warnings (`pytest.ini` turns them into
errors); `openapi.json` and types regenerated if the API changes; with no packages installed,
behaviour identical to before.

### 9.4 Later phases, only if wanted

| Phase | Content | Needs a design decision from you |
|---|---|---|
| 2 | Registry for sources and resolvers (entry points + `Protocol`); built-in scrapers as registered sources | yes, on the principle |
| 3 | Proposals queue (`kind='source'` first); `Notice`; webhook as a notifier | yes |
| 4 | `ServiceSpec` list for the service panel | yes (small) |
| 5 | `PlaybackEngine` and `engine_for` (after `stream_kind`) | yes |
| 6 | Guide role; `/api/v1/epg/xml` compared before and after | yes |
| 7 | Output role; m3u, HDHomeRun and web player as built-ins; Xtream as the first new one | yes |

If the registry is not wanted here, the load hooks are few and small and would stay in our fork on
top of the pipeline.

---

## 10. Output experiments

Run on an x86 test bench with a local Kodi 22 (desktop). Nothing was measured on low-power hardware;
timings, load and UI blocking there remain unknown. Throwaway bridges, standard library only,
with sample data.

| Output | Client | Works | Guide | Gain over IPTV Simple | Effort in the scraper | Verdict |
|---|---|---|---|---|---|---|
| m3u + XMLTV (today) | IPTV Simple | yes | yes | baseline | none | stays |
| HDHomeRun | `pvr.hdhomerun` | only with a UDP 65001 responder (measured) | no: only SiliconDust's cloud (measured) | almost none | medium | not now |
| Stalker | `pvr.stalker` | yes (measured) | yes, `get_epg_info` | none in Kodi | 1-1.5 days (estimate) | only for a device that speaks only Stalker |
| Xtream | IPTV Simple (`m3u_plus` + `xmltv.php`) | yes (measured) | yes | none in Kodi; opens phone and TV apps | 0.5-1 day (estimate) | yes, after testing with a real app |
| TVHeadend | `pvr.hts` | with limitations (measured) | yes, via its XMLTV socket | 3-5x faster zapping, timeshift and recording inside TVHeadend | high | test bench only for now |

**HDHomeRun.** As is, the add-on discovers over binary UDP 65001 and finds nothing: 0 channels after
60 s and no error line in `kodi.log`. With a ~40-line test responder, Kodi loaded all 150 lineup
channels in under a minute, ignored `GuideNumber` (numbered 1..150), and used no logos or groups.
Opening took 1.4-7.9 s (median ~3.5 s, 3 HTTP channels, 10 opens). Guide data only comes from
`api.hdhomerun.com` with a `DeviceAuth`; with it empty the cloud returns 403, and Kodi goes to the
internet even with a local tuner. The scraper cannot fix that.

**Stalker and Xtream.** Both worked: 152 channels, 22 groups, guide data, picture verified by the
sha1 of two consecutive screenshots, 24 API checks with no failure. Open times (three takes, not
statistics): Stalker 3.16 / 2.19 / 8.14 s; Xtream via IPTV Simple 4.64 / 4.12 / 7.79 s; the ~8 s
third take was Kodi waiting on the previous player to close. **Finding:** Kodi opens each stream
**twice** (HEAD, a 128 KiB GET it closes, then the real GET). Harmless for HTTP; for AceStream through
an output that does not set `#KODIPROP:mimetype`, each zap **could** cost two engine sessions.
Deduced, not measured.

**TVHeadend 4.3** in a container, `pvr.hts`, with three free-to-air HTTP channels and one FAST
channel, no AceStream:
- zapping 1.2-4.7 s vs 6.7-14.4 s with IPTV Simple; cold open 1.3-5.6 s vs 6.5-15.2 s;
- it **opens sources on its own**: the initial scan and over-the-air EPG grabbing subscribed to the
  muxes 16 times in 30 min with four channels, one held for 10+ min. With AceStream those would be
  engine sessions nobody watches. Turned off per mux (EIT/PSIP off, `skipinitscan`, `epg=0`), which
  must be automated because the default turns it back on;
- loses per-channel headers (only `-user_agent` in the `ffmpeg` wrapper), `#KODIPROP` and filtering;
  HLS URLs must be wrapped in `pipe:///usr/bin/ffmpeg … -c copy`;
- crashed in a loop with `--network host` when it found an HDHomeRun on the LAN; not with an
  isolated network;
- x86 usage up to 12.7 % of a core and 132 MB RAM, plus ~50 MB per active `ffmpeg`.

TVHeadend's timeshift and recording are its own; they do not change the scraper's pass-through
design ("No seek" in the known limitations).

---

## 11. Out of scope

- File downloads (eD2k, Usenet, IRC file bots), Sopcast, PPStream.
- Anything that decodes or bypasses third-party protection.
- Sports events.
- Sources with encrypted links unless their publisher offers an API.
- Examples the roles admit but nobody is building: DVB tuners, Matrix, RSS/Atom, PeerTube.
- Later and optional: a ranking hook (`rank(candidates, context)` in `stream_ranking.py`) that only
  reorders within today's relay rules, with a short deadline and today's order as fallback; and
  local models for recipe proposals, always outside the playback path.

---

## 12. Open questions for the maintainer

1. **Phase 1 as described:** one PR, no behaviour change, with the per-path table and pinning tests?
2. **Small PRs:** welcome, and one PR each?
3. **A plugin registry in this repo at all?** If not, phase 1 still stands alone.
4. **Events and `url_scraping`:** any planned change that phase 1 should wait for?
5. **Where the contract would live** if phases 2+ happen: a folder in this repo, a separate package,
   or both?
6. **Remote players and media servers:** outputs, or left outside the roles? Our suggestion: outside
   until there are two outputs in a registry.
7. **Trusted discoverers:** does turning one on count as prior approval? If so, should created
   sources start paused?
8. **Module options and `runtime-config.json`:** a separate section in that file, the `settings`
   table, or neither?

---

## Appendix: design decisions and rationale

| Decision | Why |
|---|---|
| Phase 1 changes no result | A refactor reviewers can verify against pinned behaviour; anything visible goes in its own PR. |
| Three import modes, not two | `manual` has `allow_excluded`, which must never reach automatic imports. |
| Keep the ZeroNet shortcut not pruning | Fixing it changes results; separate PR. |
| Pipeline runs in the caller's session | Telegram's cursor must commit with its rows. |
| Partial reads write and never prune | Otherwise one failing branch of a source deletes that branch's channels. |
| New status strings deferred | The UI only paints the exact `'OK'` green. |
| Deadlines discard, never cut | Python threads cannot be killed; real limits need a subprocess. |
| Modules are not sandboxed | Same process; review, contract and conformance suite instead. |
| A package never replaces a built-in | Predictable behaviour; built-ins follow the same contract. |
| Only enabled modules are imported | A broken disabled package cannot break start-up. |
| Code changes need a restart | Python does not unload modules; only enable/disable can be live. |
| Discoveries go to a queue, approved by a person | Anything that changes a source needs a human decision. |
| Guide is its own role | Different output, cycle and failure impact from sources. |
| Outputs migrate last | Most coupled to the relay and the player. |
| Engines keep ownership rules in the core | They are project invariants, not per-engine behaviour. |
| No container control in the scraper | The scraper has no Docker socket and should keep it that way. |
| Secrets stay as documented today | Plain-text storage is a declared constraint, not a TODO. |
