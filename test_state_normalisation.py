import asic_stats


def test_state_normalisation_full_names_and_codes():
    assert asic_stats._state("New South Wales") == "NSW"
    assert asic_stats._state("Queensland") == "QLD"
    assert asic_stats._state("Western Australia") == "WA"
    assert asic_stats._state("South Australia") == "SA"
    assert asic_stats._state("Northern Territory") == "NT"
    assert asic_stats._state("Victoria") == "VIC"
    assert asic_stats._state("Tasmania") == "TAS"
    assert asic_stats._state("Australian Capital Territory") == "ACT"


def test_unknown_state_is_not_guessed():
    assert asic_stats._state("Australia") == ""
