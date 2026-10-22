"""/app/crypto uses only real sources: a down source is skipped, and with no
source at all the answer is an error, never invented prices."""
import pytest

from proxy import endpoints as E


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts and ends with no source rested and nothing cached."""
    yield
    E._crypto_source_down.clear()
    E._crypto_last_good.clear()
    E.cache.clear()


def _fake_json(answers):
    def fetch(url):
        host = url.split('/')[2]
        if host not in answers:
            raise OSError('unreachable')
        return answers[host](url)
    return fetch


def _binance(url):
    if '/ticker/24hr' in url:
        return {"lastPrice": "100.0", "openPrice": "90.0", "highPrice": "110.0", "lowPrice": "80.0",
                "quoteVolume": "5000000"}
    return [[0, "1", "1", "1", str(80 + i)] for i in range(8)]   # klines: close is index 4


def _reset(monkeypatch, answers):
    E._crypto_source_down.clear()
    E._crypto_last_good.clear()
    E.cache.clear()
    monkeypatch.setattr(E, '_crypto_json', _fake_json(answers))


def test_binance_used_when_okx_and_huobi_are_down(monkeypatch):
    _reset(monkeypatch, {'data-api.binance.vision': _binance})
    d = E.get_crypto(view='all', symbols='BTC', fiat='usd')
    kinds = [i['t'] for i in d['items']]
    assert 'c' in kinds and 'trend' in kinds and 'k' in kinds
    coin = next(i for i in d['items'] if i['t'] == 'c')
    assert coin['b'] == '100' and coin['c'] == '+11.11'
    assert 'okx' in E._crypto_source_down and 'huobi' not in E._crypto_source_down  # huobi not needed


def test_no_source_is_an_error_not_demo_data(monkeypatch):
    _reset(monkeypatch, {})
    status, _, body = E.handle_app_query('/app/crypto?view=all&symbols=BTC,ETH')
    assert status == 502
    assert '演示' not in body.decode('utf-8')
