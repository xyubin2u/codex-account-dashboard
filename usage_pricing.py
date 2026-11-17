"""Verified OpenAI Standard API reference rates, USD per million tokens.

Snapshot: 2026-11-09. Exact model IDs only. API-equivalent estimates use the
30-minute cache-write rate where published. No Fast/regional/tool surcharge.
"""

CHECKED_AT = '2026-11-09'
SOURCE = 'https://developers.openai.com/api/docs/pricing'
FIELDS = ('input', 'cached_input', 'cache_write', 'output')


def _price(short, long=None, url=SOURCE):
    return dict(rates=dict(zip(FIELDS, short)),
                long_context=dict(zip(FIELDS, long)) if long else None,
                context_threshold=272000 if long else None, url=url,
                checked_at=CHECKED_AT, tier='standard',
                cache_write_ttl='30m' if short[2] is not None else None)


OFFICIAL_PRICES = {
    'gpt-6-astra': _price((10, 1, 12.5, 50), (20, 2, 25, 75)),
    'gpt-6.1-sol': _price((2, .1, 2.5, 10), (4, .2, 5, 15)),
    'gpt-6.1-terra': _price((1, .05, 1.25, 5), (2, .1, 2.5, 7.5)),
    'gpt-6-sol': _price((2, .2, 2.5, 10), (4, .4, 5, 15)),
    'gpt-6-luna': _price((.1, .01, .125, .5), (.2, .02, .25, .75)),
    'gpt-5.6-sol': _price((4, .4, 5, 20), (8, .8, 10, 30)),
    'gpt-5.6-terra': _price((2, .2, 2.5, 12), (4, .4, 5, 18)),
    'gpt-5.6-luna': _price((.2, .02, .25, 1.2), (.4, .04, .5, 1.8)),
    'gpt-5.5': _price((5, .5, None, 30), (10, 1, None, 45)),
    'gpt-5.4': _price((2.5, .25, None, 15), (5, .5, None, 22.5)),
    'gpt-5.4-mini': _price((.75, .075, None, 4.5)),
    'gpt-5.2': _price((1.75, .175, None, 14)),
    'gpt-5.3-codex': _price((1.75, .175, None, 14)),
    'gpt-5.1-codex-mini': _price((.25, .025, None, 2), url=
        'https://developers.openai.com/api/docs/models/gpt-5.1-codex-mini'),
}
