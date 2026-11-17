"""Official supplier API reference prices for native AGY model IDs.

Verified 2026-11-14 against the linked primary price pages. USD per million
text tokens, Standard tier, not subscription debits or actual AGY bills.
Exact allowlisted aliases use the corresponding public API model as a reference;
this is a documented mapping, not a claim about identical backend deployment.
Reference snapshot applies to all stored dates, matching the existing GPT view.
Google's published future changes are metadata only; this snapshot is not a
background price updater. No tool, cache storage, regional or fast-mode fees.
"""
from copy import deepcopy

CHECKED_AT='2026-11-14'
GOOGLE='https://ai.google.dev/gemini-api/docs/pricing'
ANTHROPIC='https://platform.claude.com/docs/en/about-claude/pricing'
FIELDS=('input','cached_input','cache_write','output')


def _price(model,rates,kind,valid_until=None):
    return {'rates':dict(zip(FIELDS,rates)),'kind':kind,'source_model':model,
        'url':GOOGLE if kind=='google_official' else ANTHROPIC,'checked_at':CHECKED_AT,
        'tier':'standard','context_threshold':None,'long_context':None,
        'cache_write_ttl':None if kind=='google_official' else '5m',
        'valid_until':valid_until,'pricing_basis':'exact','mapping_note':''}


CANONICAL={
    'gemini-3.8-flash':_price('gemini-3.8-flash',(.75,.075,None,3.75),'google_official','2026-12-31'),
    'gemini-4-argon':_price('gemini-4-argon',(2,.1,None,10),'google_official','2026-11-30'),
    'gemini-4-hydrogen':_price('gemini-4-hydrogen',(1.5,.075,None,7.5),'google_official'),
    'claude-opus-4-6':_price('claude-opus-4-6',(5,.5,6.25,25),'anthropic_official'),
    'claude-sonnet-4-6':_price('claude-sonnet-4-6',(3,.3,3.75,15),'anthropic_official'),
    'claude-opus-5-5':_price('claude-opus-5-5',(4,.2,5,20),'anthropic_official'),
    'claude-sonnet-5-5':_price('claude-sonnet-5-5',(2,.1,2.5,10),'anthropic_official'),
}
ALIASES={
    'gemini-3.8-flash-n':'gemini-3.8-flash',
    'gemini-3.8-flash-tiered':'gemini-3.8-flash',
    'gemini-4-argon-trial':'gemini-4-argon',
    'claude-opus-4-6-thinking':'claude-opus-4-6',
    **{f'claude-{family}-5-5-{effort}':f'claude-{family}-5-5'
       for family in ('opus','sonnet') for effort in ('low','medium','high')},
}


def _catalog():
    prices={}
    for model,canonical in {**{m:m for m in CANONICAL},**ALIASES}.items():
        reference=deepcopy(CANONICAL[canonical]);notes=[]
        if model!=canonical:
            reference['pricing_basis']='mapped_official'
            notes.append(f'AGY 别名按 {canonical} 官方 API 价格参考映射，非原生订阅账单')
        if reference['cache_write_ttl']:
            notes.append('缓存写入按官方 5m 参考价；原日志未报告 TTL，不还原实际缓存账单')
        else:
            notes.append('缓存读取按官方价；不含缓存存储时长、工具或区域费用')
        if reference['valid_until']:
            notes.append('当前公布优惠价至 '+reference['valid_until']+'；本机保存核定日参考快照，不自动联网更新')
        reference['mapping_note']='；'.join(notes)
        prices[model]=reference
    return prices


OFFICIAL_AGY_PRICES=_catalog()
