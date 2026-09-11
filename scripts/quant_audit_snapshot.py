"""Offline snapshot audit; reads a trusted local state/cache and fill ledger.

No broker, network, strategy trial, registry write, or environment file load.
Clock returns below use completed UTC weekdays, NOT an exchange calendar.
Mirror gross_over_nav uses snapshot NAV, not contemporaneous daily NAV.
"""
import argparse, json, pickle, hashlib, collections, sys
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.mirror_reconcile import summarize, load_ledger
from backtest.metrics import paired_test, sharpe_ratio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    P=args.evidence_dir
    s=json.loads((P/'data/state.json').read_text())
    eq,cx,vix=pickle.loads((P/'hist_cache.pkl').read_bytes())
    out={'as_of':s['last_tick'],'clock_start':s['clock_start'],'clock_resets':len(s['clock_resets']),
         'snapshot_sha256':hashlib.sha256((P/'data/state.json').read_bytes()).hexdigest(),
         'reported_pair':s['s3_vs_s1'],'reported_common_pair':s['s3_vs_s1_common'],'books':{}}
    last_day=s['last_tick'][:10]
    for k,v in s['systems'].items():
     h=s['equity_history'][k]; daily=pd.Series({t[:10]:float(val) for t,val in h}).sort_index()
     closed=daily[daily.index<last_day]
     wr=closed[closed.index>=s['clock_start']]; wr=wr[pd.to_datetime(wr.index).weekday<5]
     returns=wr.pct_change().dropna()
     weights=v['weights']; cweights={a:b for a,b in weights.items() if '/' in a}
     info={'nav':v['equity'],'inception':h[0][0],'reported_inception_return':v['equity']/s['nav0']-1,
           'last_completed_day':closed.index[-1],'clock_return':wr.iloc[-1]/wr.iloc[0]-1,
           'clock_vol':returns.std()*np.sqrt(252),'clock_n_weekday_returns':len(returns),
           'clock_drawdown':float((wr/wr.cummax()-1).min()),'gross':sum(map(abs,weights.values())),
           'net':sum(weights.values()),'crypto_gross':sum(map(abs,cweights.values())),
           'crypto_short':sum(-v for v in cweights.values() if v<0),
           'cost_paid_usd_counter':s['cost_paid_usd'][k], 'counter_start_not_recorded':True,
           'cumulative_l1_turnover':s['turnover_l1_cum'][k]}
     violations=[]
     for sym,wt in weights.items():
      adv=(s.get('cost_inputs',{}).get(sym) or [0])[0]
      if adv>0 and abs(wt)*v['equity']>adv*.1:
       violations.append({'symbol':sym,'notional':abs(wt)*v['equity'],'adv':adv,'position_to_adv':abs(wt)*v['equity']/adv})
     info['liquidity_violations']=sorted(violations,key=lambda x:x['position_to_adv'],reverse=True)
     out['books'][k]=info
    # Common complete-day period, no calendar-session inference beyond explicit label.
    by={k:pd.Series({t[:10]:float(v) for t,v in s['equity_history'][k]}) for k in ('s1','s3')}
    common=pd.DataFrame(by).sort_index().loc[s['clock_start']:]
    common=common[(common.index<last_day)&(pd.to_datetime(common.index).weekday<5)].dropna()
    r=common.pct_change().dropna()
    out['corrected_hac_completed_weekdays']=paired_test(r.s3,r.s1)
    out['llm_budget']=s['llm_budget']
    out['incidents']={'retained_count':len(s['data_incidents']),'by_kind':dict(collections.Counter(x['kind'] for x in s['data_incidents'])),
                      'last':s['data_incidents'][-1]}
    merged={**{k:v for k,v in eq.items() if k!='SPY'},**cx}
    cl=pd.DataFrame({k:v.close for k,v in merged.items()}).sort_index(); logr=np.log(cl/cl.shift(1)).dropna()
    out['covariance']={'panel_rows':len(cl),'symbols':len(cl.columns),'complete_return_rows':len(logr),
                       'first_complete':str(logr.index.min()),'last_complete':str(logr.index.max()),
                       'newest_listings':sorted([(len(v),k,str(v.index.min().date())) for k,v in eq.items()])[:10]}
    out['mirror']=summarize(load_ledger(P/'data/fills_history.jsonl'),s)
    args.output.write_text(json.dumps(out, indent=2, default=str) + '\n')
    print(f'Wrote {args.output}; snapshot {out["as_of"]}')


if __name__ == "__main__":
    main()
