"""
tests/test_dnd5_combat.py
Smoke test: start a D&D SRD 5.2 session, load a 1 Fighter vs 2 Goblin battle
campaign, run the fight to completion, and assert the session ends correctly.

Run directly:
    pytest tests/test_dnd5_combat.py -v

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

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dnd5_campaign.json"

WIZARD_INPUTS: dict = {
    "name": "Test Fighter",
    "race": "human",
    "class": "fighter",
    "background": "soldier",
    "level": 1,
    "ability_scores": {
        "str": 16, "dex": 12, "con": 14,
        "int": 10, "wis": 11, "cha": 10,
    },
    "skill_choices": ["athletics", "perception"],
    "fighting_style": "defense",
    "hit_die_choice": "max",
}


# ── session fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def dnd5_session(gm_client: GMClient) -> str:
    """
    Creates a D&D SRD 5.2 session, loads the campaign, and returns the session sid.
    Wizard is completed automatically if triggered.
    """
    # 1. Create session
    resp = gm_client.post("/v1/sessions", {"ruleset_id": "dnd_srd_5_2_1"})
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
        print("  Wizard triggered — completing D&D character creation")
        complete_wizard(gm_client, session_sid, WIZARD_INPUTS)
        phase = get_flow_phase(gm_client, session_sid)
        assert phase == "scene_setup", (
            f"Expected scene_setup after wizard completion, got {phase!r}"
        )

    return session_sid


# ── tests ──────────────────────────────────────────────────────────────────────

def test_scene_becomes_active(gm_client: GMClient, dnd5_session: str) -> None:
    """Scene must transition from scene_setup to awaiting_player_action."""
    wait_for_phase(gm_client, dnd5_session, "awaiting_player_action")
    phase = get_flow_phase(gm_client, dnd5_session)
    assert phase == "awaiting_player_action", (
        f"Expected awaiting_player_action, got {phase!r}"
    )


def test_battle_scene_configuration(gm_client: GMClient, dnd5_session: str) -> None:
    """Battle scene must be battle_map, round 1, with active actor and attack affordances."""
    wait_for_phase(gm_client, dnd5_session, "awaiting_player_action")

    resp = gm_client.get(f"/v1/sessions/{dnd5_session}/flow")
    _assert_2xx(resp, "GET /v1/sessions/{sid}/flow")
    snapshot = resp.json()

    scene = snapshot["scene_summary"]
    assert scene["mode"] == "battle_map", (
        f"Expected mode=battle_map, got {scene['mode']!r}"
    )
    assert scene.get("round") is not None, "Round counter must be set"
    assert scene["round"] == 1, (
        f"D&D encounter should start at round 1, got {scene['round']}"
    )
    assert scene.get("active_actor_sid"), "active_actor_sid must be set"

    attack_actions = [
        a for a in snapshot.get("available_actions", [])
        if a["action_type"] == "attack"
    ]
    assert len(attack_actions) >= 1, (
        "At least one attack action must be available (goblins must be present)"
    )

    # Confirm two target candidates (2 goblins in the fixture)
    all_targets = [
        t
        for a in attack_actions
        for t in a.get("suggestions", {}).get("target_sid", [])
    ]
    assert len(all_targets) == 2, (
        f"Expected 2 goblin targets, found {len(all_targets)}: {all_targets}"
    )


def test_initiative_step_is_set(gm_client: GMClient, dnd5_session: str) -> None:
    """Scene resource must expose an initiative_step."""
    wait_for_phase(gm_client, dnd5_session, "awaiting_player_action")

    resp = gm_client.get(f"/v1/sessions/{dnd5_session}/scene")
    _assert_2xx(resp, "GET /v1/sessions/{sid}/scene")
    scene = resp.json()

    initiative_step = (
        scene.get("initiative_step")
        or scene.get("scene_summary", {}).get("initiative_step")
    )
    assert initiative_step is not None, (
        f"initiative_step must be set; scene keys: {list(scene)}"
    )
    print(f"  initiative_step: {initiative_step}")


def test_combat_runs_to_completion(gm_client: GMClient, dnd5_session: str) -> None:
    """Combat loop must resolve with scene_transition or campaign_end."""
    wait_for_phase(gm_client, dnd5_session, "awaiting_player_action")
    final_phase = run_combat_to_completion(gm_client, dnd5_session, max_rounds=30)
    assert final_phase in ("scene_transition", "campaign_end"), (
        f"Expected scene_transition or campaign_end, got {final_phase!r}"
    )


def test_final_snapshot_has_no_errors(gm_client: GMClient, dnd5_session: str) -> None:
    """Final UIFlowSnapshot must carry no errors."""
    wait_for_phase(gm_client, dnd5_session, "awaiting_player_action")
    run_combat_to_completion(gm_client, dnd5_session, max_rounds=30)

    resp = gm_client.get(f"/v1/sessions/{dnd5_session}/flow")
    _assert_2xx(resp, "GET /v1/sessions/{sid}/flow (final)")
    errors = resp.json().get("errors", [])
    assert errors == [], f"Unexpected errors in final snapshot: {errors}"


def test_journal_has_entries_after_combat(gm_client: GMClient, dnd5_session: str) -> None:
    """Journal must contain entries after a completed combat."""
    wait_for_phase(gm_client, dnd5_session, "awaiting_player_action")
    run_combat_to_completion(gm_client, dnd5_session, max_rounds=30)

    resp = gm_client.get(f"/v1/sessions/{dnd5_session}/journal", params={"limit": 5})
    _assert_2xx(resp, "GET /v1/sessions/{sid}/journal")
    data = resp.json()

    # Accept either a plain list or an object with an "entries" key
    entries = data if isinstance(data, list) else data.get("entries", [])
    assert len(entries) > 0, "Journal must have at least one entry after combat"
    print(f"  journal entries returned: {len(entries)}")
