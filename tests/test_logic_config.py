import pytest

from herdr_talkback.logic_config import parse_logic_config


def test_opt_in_and_defaults():
    assert not parse_logic_config({}).enabled
    cfg = parse_logic_config({'enabled': True, 'monitor_pane': 'monitor', 'targets': ['worker']})
    assert cfg.poll_seconds == cfg.stable_seconds == cfg.observation_seconds == 10
    assert cfg.timeout_seconds == cfg.cooldown_seconds == 120
    assert cfg.max_continues_per_30m == 3


@pytest.mark.parametrize('raw', [
    {'enabled': 'yes'}, {'enabled': True}, {'targets': 'worker'},
    {'targets': ['a', 'a']}, {'targets': ['']}, {'poll_seconds': 0},
    {'stable_seconds': -1}, {'timeout_seconds': True}, {'cooldown_seconds': float('inf')},
    {'max_continues_per_30m': 1.5}, {'enabled': True, 'monitor_pane': 'a', 'targets': ['a']},
    {'unknown_option': 1},
])
def test_rejects_invalid_config(raw):
    with pytest.raises(ValueError):
        parse_logic_config(raw)
