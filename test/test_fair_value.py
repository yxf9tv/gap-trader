"""Tests for M1 — Fair-Value Service + Anchor-Trust Gate (T1–T7)."""

import pytest
from fair_value import compute_fair_line, FairLine, FairValueConfig


# T1: cents + devig (2-way)
# Pinnacle −110 / −110 → both implied ≈ 52.4¢, devigged fair ≈ 50.0¢ each
def test_t1_cents_and_devig_2way():
    quotes = {
        "Over": {"pinnacle": -110},
        "Under": {"pinnacle": -110},
    }
    result = compute_fair_line(quotes, FairValueConfig(
        allow_single_sharp=True, gate_floor_2way=0.0,
    ))
    assert result.gate_pass
    assert pytest.approx(result.per_side["Over"], 0.1) == 50.0
    assert pytest.approx(result.per_side["Under"], 0.1) == 50.0
    assert pytest.approx(result.per_side["Over"] + result.per_side["Under"], 0.1) == 100.0
    assert result.sharps_used == ["pinnacle"]


# T2: American sign handling
# dog at +150 and fav at −180 → fav ≈ 62–64¢, dog ≈ 36–38¢, sum 100¢
def test_t2_american_sign_handling():
    quotes = {
        "Fav": {"pinnacle": -180},
        "Dog": {"pinnacle": 150},
    }
    result = compute_fair_line(quotes, FairValueConfig(allow_single_sharp=True))
    assert result.gate_pass
    assert 61 <= result.per_side["Fav"] <= 64
    assert 36 <= result.per_side["Dog"] <= 39
    assert pytest.approx(result.per_side["Fav"] + result.per_side["Dog"], 0.1) == 100.0
    assert result.per_side["Dog"] < result.per_side["Fav"]


# T3: agreement gate skip
# Pinnacle makes A the favorite, Novig makes B the favorite
def test_t3_agreement_gate_skip():
    # Pinnacle: A -120 (~54.5¢ implied), B +100 (50.0¢ implied) → A is favorite
    # Novig: A +100 (50.0¢ implied), B -120 (~54.5¢ implied) → B is favorite
    quotes = {
        "A": {"pinnacle": -120, "novig": 100},
        "B": {"pinnacle": 100, "novig": -120},
    }
    result = compute_fair_line(quotes)
    assert not result.gate_pass
    assert "directional disagreement" in result.reason
    assert "pinnacle" in result.sharps_used
    assert "novig" in result.sharps_used


# T4: fall silently
# Only Novig + ProphetX present (Pinnacle absent) → consensus from the two
def test_t4_fall_silently():
    # Clear favorite (>51%) to pass the floor gate
    quotes = {
        "Over": {"novig": -130, "prophetx": -125},
        "Under": {"novig": 110, "prophetx": 105},
    }
    result = compute_fair_line(quotes)
    assert result.gate_pass
    assert set(result.sharps_used) == {"novig", "prophetx"}
    assert pytest.approx(result.per_side["Over"] + result.per_side["Under"], 0.5) == 100.0
    assert result.favorite == "Over"
    assert "pinnacle" not in result.sharps_used


# T5: 3-way soccer floor
# favorite consensus 48% (Home 48 / Draw 28 / Away 24)
# With 2-way floor (51%) → skip (3-way default floor is 0, so passes)
# With 3-way floor set to 51% → skip
def test_t5_3way_soccer_floor():
    # Decimal odds that imply home 48% (2.083), draw 28% (3.571), away 24% (4.167)
    quotes = {
        "Home": {"pinnacle": 2.083},
        "Draw": {"pinnacle": 3.571},
        "Away": {"pinnacle": 4.167},
    }

    # Default config: 3-way floor is 0 (off) → pass
    config_pass = FairValueConfig(
        sport="soccer", odds_format="decimal", allow_single_sharp=True,
    )
    result_pass = compute_fair_line(quotes, config_pass)
    assert result_pass.gate_pass
    assert result_pass.favorite == "Home"

    # 3-way floor set to 51% → skip (Home at 48% < 51%)
    config_skip = FairValueConfig(
        sport="soccer", gate_floor_3way=0.51, odds_format="decimal",
        allow_single_sharp=True,
    )
    result_skip = compute_fair_line(quotes, config_skip)
    assert not result_skip.gate_pass
    assert "floor" in result_skip.reason


# T6: conservative anchor vs median
# on the bet side, Pinnacle 60¢ / ProphetX 55¢ / Novig 57¢
# default (conservative) → 55¢; median → 57¢
def test_t6_anchor_mode():
    # Prices as cents (already fair, no vig)
    quotes = {
        "Over": {"pinnacle": 60.0, "prophetx": 55.0, "novig": 57.0},
        "Under": {"pinnacle": 40.0, "prophetx": 45.0, "novig": 43.0},
    }
    config_conservative = FairValueConfig(odds_format="cents")
    result_con = compute_fair_line(quotes, config_conservative)
    assert result_con.gate_pass
    assert pytest.approx(result_con.per_side["Over"], 0.1) == 55.0
    assert pytest.approx(result_con.per_side["Under"], 0.1) == 40.0

    config_median = FairValueConfig(odds_format="cents", anchor_mode="median")
    result_med = compute_fair_line(quotes, config_median)
    assert result_med.gate_pass
    assert pytest.approx(result_med.per_side["Over"], 0.1) == 57.0
    assert pytest.approx(result_med.per_side["Under"], 0.1) == 43.0


# T7: n = 1 policy
# Only Pinnacle present → default gate_pass=False, reason="single-source"
# With allow_single flag → pass
def test_t7_n1_policy():
    # Prices with clear favorite (>51%) so floor doesn't interfere with gate test
    quotes = {
        "Fav": {"pinnacle": -200},
        "Dog": {"pinnacle": 170},
    }

    result_default = compute_fair_line(quotes)
    assert not result_default.gate_pass
    assert result_default.reason == "single-source"

    config_allow = FairValueConfig(allow_single_sharp=True)
    result_allow = compute_fair_line(quotes, config_allow)
    assert result_allow.gate_pass
