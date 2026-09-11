"""Compare legacy and causal event timing on the original trusted local cache.

This diagnostic does not fetch data, run the lockbox, or change the registry.
The archive must be the audit's original source snapshot, not an unknown file.
"""
import argparse
import ast
import io
import json
import pickle
import sys
import tarfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest.metrics import sharpe_ratio
from research.primitives import _cost_model, calendar_time_daily


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tarfile.open(args.original_source) as archive:
        members = {m.name.removeprefix('./'): m for m in archive.getmembers() if m.isfile()}
        old_source = archive.extractfile(members['research/primitives.py']).read().decode()
        events = pd.read_csv(io.BytesIO(archive.extractfile(
            members['research/h1_events.csv']).read()), parse_dates=['date'])
        prices = pickle.loads(archive.extractfile(members['research/h1_prices.pkl']).read())

    # Execute only the archived calculation, not the old module's imports.
    old_function = next(node for node in ast.parse(old_source).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == 'calendar_time_daily')
    namespace = {'np': np, 'pd': pd}
    exec(compile(ast.Module(body=[old_function], type_ignores=[]),
                 '<archived calendar_time_daily>', 'exec'), namespace)
    old, _ = namespace['calendar_time_daily'](events, prices, 2, 10, _cost_model())
    new, _ = calendar_time_daily(events, prices, 2, 10, _cost_model())
    # The price cache includes a year for the market-model estimation window.
    # Evaluate both calculations on the same cached event window, not that
    # leading year of zero positions.
    start, end = events['date'].min(), events['date'].max()
    old, new = old.loc[start:end], new.loc[start:end]
    result = {
        'purpose': 'audit of timing on existing cached research data; not a new strategy trial or rerun of lockbox',
        'n_events': len(events), 'start': str(old.index.min()),
        'end': str(old.index.max()), 'n_days': len(old),
        'legacy_sharpe': float(sharpe_ratio(old)),
        'causal_sharpe': float(sharpe_ratio(new)),
        'legacy_total_return': float((1 + old).prod() - 1),
        'causal_total_return': float((1 + new).prod() - 1),
        'changed_days': int((~np.isclose(old, new)).sum()),
    }
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
