"""Schools loader + overlay tests — offline."""
from __future__ import annotations
import pandas as pd
from reip.loaders import schools


def test_aggregate_handles_empty():
    out = schools._aggregate([])
    assert out.empty


def test_aggregate_handles_unmappable_zip():
    """Records without a usable zip should drop out cleanly."""
    recs = [{"zip_location": "", "school_level": 1, "enrollment": 100}]
    out = schools._aggregate(recs)
    assert out.empty


def test_aggregate_groups_and_classifies_levels():
    recs = [
        {"zip_location": "38018", "school_level": 1, "enrollment": 400, "charter": 0, "teachers_fte": 25},
        {"zip_location": "38018", "school_level": 1, "enrollment": 350, "charter": 0, "teachers_fte": 22},
        {"zip_location": "38018", "school_level": 2, "enrollment": 600, "charter": 0, "teachers_fte": 35},
        {"zip_location": "38018", "school_level": 3, "enrollment": 1100, "charter": 1, "teachers_fte": 60},
        # different zip
        {"zip_location": "38120", "school_level": 1, "enrollment": 250, "charter": 0, "teachers_fte": 18},
    ]
    out = schools._aggregate(recs).set_index("zip")
    assert int(out.loc["38018", "school_count"]) == 4
    assert int(out.loc["38018", "elementary_count"]) == 2
    assert int(out.loc["38018", "middle_count"]) == 1
    assert int(out.loc["38018", "high_count"]) == 1
    assert int(out.loc["38018", "charter_count"]) == 1
    assert int(out.loc["38018", "total_enrollment"]) == 2450
    # weighted st/teacher ratio: 2450 / 142 ≈ 17.3
    assert 16 < out.loc["38018", "avg_student_teacher_ratio"] < 18
    assert int(out.loc["38120", "school_count"]) == 1


def test_aggregate_zfills_short_zips():
    """NCES zips show up as ints sometimes; we must zfill to 5."""
    recs = [{"zip_location": 7039, "school_level": 1, "enrollment": 200, "charter": 0, "teachers_fte": 12}]
    out = schools._aggregate(recs).set_index("zip")
    assert "07039" in out.index
