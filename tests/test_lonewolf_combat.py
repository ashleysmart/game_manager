"""
tests/test_lonewolf_combat.py
Smoke test: start a Lone Wolf session, load a 1-player vs 2-enemy battle
campaign, run the fight to completion, and assert the session ends correctly.

Run directly:
    pytest tests/test_lonewolf_combat.py -v

Or via the runner:
    python tests/run_smoke_tests.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import (
    GMClient,
    _assert_2xx,
    complete_wizard,
    get_flow,
    get_flow_phase,
    run_combat_to_completion,
    wait_for_phase,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "lonewolf_campaign.json"

WIZARD_INPUTS: dict = {
    "name": "Lone Wolf",
    "combat_skill": 14,
    "endurance": 25,
    "weapon_choice": "sword",
    "disciplines": ["weaponskill", "hunting"],
}


# ── session fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def lone_wolf_session(gm_client: GMClient) -> str:
    """
    Creates a Lone Wolf session, loads the campaign, and returns the session sid.
    Wizard is completed automatically if triggered.
    """
    # 1. Create session
    resp = gm_client.post("/v1/sessions", {"ruleset_id": "lone_wolf"})
    _assert_2xx(resp, "POST /v1/sessions")

    body = resp.json()
    session_sid: str = body["sid"]
    assert session_sid, "session sid must be present in POST /v1/sessions response"
    print(f"\n  session sid: {session_sid}")

    # 2. Load campaign
    campaign = json.loads(FIXTURE_PATH.read_text())
    resp = gm_client.post(f"/v1/sessions/{session_sid}/campaign", campaign)
    _assert_2xx(resp, "POST /v1/sessions/{sid}/campaign")

    initial_phase: str = resp.json()["status"]
    assert initial_phase, "UIFlowSnapshot.status must be present after campaign load"
    print(f"  initial phase: {initial_phase}")

    # 3. Complete wizard if triggered
    if initial_phase == "wizard_active":
        print("  Wizard triggered — completing character creation")
        complete_wizard(gm_client, session_sid, WIZARD_INPUTS)
        phase = get_flow_phase(gm_client, session_sid)
        assert phase == "scene_setup", (
            f"Expected scene_setup after wizard completion, got {phase!r}"
        )

    return session_sid


# ── tests ──────────────────────────────────────────────────────────────────────

def test_scene_becomes_active(gm_client: GMClient, lone_wolf_session: str) -> None:
    """Scene must transition from scene_setup to awaiting_player_action."""
    wait_for_phase(gm_client, lone_wolf_session, "awaiting_player_action")
    phase = get_flow_phase(gm_client, lone_wolf_session)
    assert phase == "awaiting_player_action", (
        f"Expected awaiting_player_action, got {phase!r}"
    )


def test_battle_scene_configuration(gm_client: GMClient, lone_wolf_session: str) -> None:
    """Battle scene must be in battle_map mode with a round counter and active actor."""
    wait_for_phase(gm_client, lone_wolf_session, "awaiting_player_action")

    resp = gm_client.get(f"/v1/sessions/{lone_wolf_session}/flow")
    _assert_2xx(resp, "GET /v1/sessions/{sid}/flow")
    snapshot = resp.json()

    scene = snapshot["scene_summary"]
    assert scene["mode"] == "battle_map", (
        f"Expected mode=battle_map, got {scene['mode']!r}"
    )
    assert scene.get("round") is not None, "Round counter must be set"
    assert scene.get("active_actor_sid"), "active_actor_sid must be set"

    attack_actions = [
        a for a in snapshot.get("available_actions", [])
        if a["action_type"] == "attack"
    ]
    assert len(attack_actions) >= 1, (
        "At least one attack action must be available (enemies must be present)"
    )


def test_combat_runs_to_completion(gm_client: GMClient, lone_wolf_session: str) -> None:
    """Combat loop must resolve with scene_transition or campaign_end."""
    wait_for_phase(gm_client, lone_wolf_session, "awaiting_player_action")
    final_phase = run_combat_to_completion(gm_client, lone_wolf_session, max_rounds=30)
    assert final_phase in ("scene_transition", "campaign_end"), (
        f"Expected scene_transition or campaign_end, got {final_phase!r}"
    )


def test_final_snapshot_has_no_errors(gm_client: GMClient, lone_wolf_session: str) -> None:
    """Final UIFlowSnapshot must carry no errors."""
    wait_for_phase(gm_client, lone_wolf_session, "awaiting_player_action")
    run_combat_to_completion(gm_client, lone_wolf_session, max_rounds=30)

    resp = gm_client.get(f"/v1/sessions/{lone_wolf_session}/flow")
    _assert_2xx(resp, "GET /v1/sessions/{sid}/flow (final)")
    errors = resp.json().get("errors", [])
    assert errors == [], f"Unexpected errors in final snapshot: {errors}"
