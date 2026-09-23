from withdraw import invite_age_days


def test_reads_age_from_card_text():
    assert invite_age_days("Eugene\nSenior iOS developer\nSent 1 hour ago\nWithdraw") == 0
    assert invite_age_days("Sent 3 days ago") == 3
    assert invite_age_days("Sent 2 weeks ago") == 14
    assert invite_age_days("Sent 1 month ago") == 30
    assert invite_age_days("Sent a year ago") == 365


def test_unknown_age_is_none_so_the_invite_is_kept():
    assert invite_age_days("Sent yesterday") is None
    assert invite_age_days("no date here") is None
