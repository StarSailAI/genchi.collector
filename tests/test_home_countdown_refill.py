"""Daily picks must refill when a corrected or expired milestone disappears."""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from genchi_product import home


@pytest.mark.parametrize('available,expected,refresh', [(8, 6, True), (5, 5, False)])
def test_refill_one_invalid_daily_pick(monkeypatch, available, expected, refresh):
    now = datetime.now(UTC)
    saved = dict(selection_date=now.astimezone(home.JST).date(),
                 milestone_ids=[str(i) for i in range(5)] + ['superseded'], updated_at=now)
    rows = [dict(id=str(i), activity_id=str(i), follow_count=0, source_count=1,
                 days_remaining=i, subject_slug=str(i), boundary='start',
                 milestone_title='开演', milestone_title_zh='开演') for i in range(available)]
    catalog = MagicMock()
    conn = catalog.connect.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = saved
    monkeypatch.setattr(home, 'candidates', lambda *_: rows)
    result = home.featured(catalog)
    assert len(result['items']) == expected
    assert 'superseded' not in {r['id'] for r in result['items']}
    writes = [c for c in conn.execute.call_args_list if 'INSERT INTO' in c.args[0]]
    assert bool(writes) is refresh
