from geo import (
    contact_location_priority,
    is_canadian_location,
    is_north_american_location,
    is_quebec_location,
    job_location_preference_score,
)


def test_geo_filters_prioritize_target_canadian_locations():
    assert is_canadian_location("Toronto, Ontario, Canada")
    assert is_north_american_location("Seattle, WA, United States")
    assert not is_quebec_location("Toronto, Ontario, Canada")
    assert is_quebec_location("Montreal, Quebec, Canada")

    assert job_location_preference_score("Toronto, ON (Hybrid)") > 0
    assert job_location_preference_score("Montreal, QC") < 0
    assert contact_location_priority("Toronto, Ontario, Canada") > contact_location_priority("")


def test_us_contacts_are_only_prioritized_when_allowed():
    assert contact_location_priority("New York, United States") == 0
    assert contact_location_priority("New York, United States", allow_north_america=True) > 0
