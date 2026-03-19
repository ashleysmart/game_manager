# GameManager

**Campaign runtime orchestrator for solo tabletop RPG play.**

GameManager is the flow controller between the player-facing UI and the pure-execution RuleEngine (RE). It decides what happens next, phases the player's interaction, assembles scenes from RE resources, routes player actions through RE, and runs hosted wizards at the right moment.

## Core Responsibilities

| # | Responsibility |
|---|---|
| 1 | Campaign runtime ownership |
| 2 | Scene lifecycle orchestration |
| 3 | Player / NPC / system turn-flow control |
| 4 | Wizard / hosted-flow orchestration |
| 5 | Deck- and tracker-driven world events |
| 6 | UI-facing flow state and prompts |
| 7 | Persistence, replay anchors, recovery |

## Architecture

```
UI
 └── GameManager service layer
       ├── CampaignRuntime          per-session domain object
       ├── FlowEngine               phase state machine
       ├── SceneOrchestrator
       │     ├── BattleMapOrchestrator
       │     ├── TravelOrchestrator
       │     └── SocialOrchestrator
       ├── WizardHost               interactive flow runner
       ├── SystemActionScheduler    timed / deferred tasks
       ├── ProjectionBuilder        UIFlowSnapshot view models
       └── ManagerRepository        CampaignRuntimeState persistence
 └── RuleEngine  (canonical world state · action pipeline · journal · saves)
```

**Invariant:** GameManager never mutates world state directly. All world changes go through RuleEngine action envelopes.

## RE Resource Mapping

GameManager concepts map to RE world-state resources:

| GM Concept | RE Resource |
|---|---|
| Plot progression | Temporal-family maps (presence state = `pending`/`active`/`completed`) |
| Encounter active | `clock.round > 0` |
| Turn order / initiative | `clock.initiative_step` + `encounter_group` members |
| Entity location | Map presence index (not an entity block) |
| Deck events | `POST /decks/{id}/draw` → RE submits card as system action |
| Time / rounds | RE clock (`PUT /world/clock`) |

## Scene Types

| Scene | Mode | RE resources assembled |
|---|---|---|
| **Battle Map** | `battle_map` | Spatial map + entities + encounter groups + trackers |
| **Travel** | `theater_of_mind` | Route tracker + supply/fatigue trackers + event/encounter/weather decks |
| **Social / Shop / Info** | `theater_of_mind` | NPC entities (dialogue/faction/inventory blocks) + reputation trackers + temporal map |

## Hosted Flows (Wizards)

| Wizard | Trigger |
|---|---|
| **Character Creation** | Campaign loaded with no player characters |
| **Level-Up** | Character XP reaches next-level threshold after a scene |

## Flow Phases

```
bootstrap → campaign_selection → [wizard_active →] scene_setup
  → awaiting_player_action ⇄ resolving_action
                           ⇄ system_step
                           → wizard_active
                           → scene_transition → scene_setup | campaign_end
```

## Public API (summary)

```
Sessions       POST /v1/sessions
               POST /v1/sessions/{id}/campaign
               GET  /v1/sessions/{id}/campaign

Flow & Scene   GET  /v1/sessions/{id}/flow           UIFlowSnapshot
               POST /v1/sessions/{id}/flow/advance
               GET  /v1/sessions/{id}/scene
               PUT  /v1/sessions/{id}/scene

Actions        POST /v1/sessions/{id}/actions         phase-gated → RE
               POST /v1/sessions/{id}/actions/suggest augmented → RE

Wizard         GET  /v1/sessions/{id}/wizard
               POST /v1/sessions/{id}/wizard/input
               POST /v1/sessions/{id}/wizard/cancel

Save / Load    POST /v1/sessions/{id}/saves           GM snapshot + RE save
               GET  /v1/sessions/{id}/saves
               POST /v1/sessions/{id}/saves/{slot}/load

Journal        GET  /v1/sessions/{id}/journal         → RE
               POST /v1/sessions/{id}/journal/replay  → RE + GM phase rebuild
```

## Implementation Phases

| Phase | Deliverable |
|---|---|
| 1 | Manager skeleton — `CampaignRuntimeState`, phase machine, campaign load, `/flow` endpoint |
| 2 | Action mediation — phase-gated submit/suggest with RE integration and correlation IDs |
| 3 | Scene orchestration — BattleMap, Travel, Social orchestrators |
| 4 | Wizard host — character creation and level-up end-to-end |
| 5 | Scheduler & deck/tracker automation — timed/system actions, travel events |
| 6 | Save / load / replay hardening — coordinated GM + RE snapshots, phase recovery |
| 7 | UI migration — switch UI from legacy commands to manager flow endpoints |

## Documentation

- [Specification](doc/Specification.md) — full design spec: architecture, domain models, RE resource mapping, flow phase machine, scene contracts, API, testing plan, observability
