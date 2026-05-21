import yfinance as yf
import pandas as pd
import numpy as np
import json
from datetime import datetime, timedelta

# Tickers
treasury_tickers = {
    '3M': '^IRX',
    '2Y': '2YY=F',
    '5Y': '5YY=F',
    '10Y': '10YY=F',
    '30Y': '30YY=F',
}
vix_ticker = '^VIX'
commodity_tickers = {
    'Gold': 'GC=F',
    'Crude Oil': 'CL=F',
    'Copper': 'HG=F',
}

all_tickers = list(treasury_tickers.values()) + [vix_ticker] + list(commodity_tickers.values())

# Fetch 10 days of data
end = datetime.now()
start = end - timedelta(days=12)

results = {}

for ticker in all_tickers:
    try:
        data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if data.empty:
            results[ticker] = {'error': 'No data'}
            continue

        close_col = 'Close'
        if isinstance(data.columns, pd.MultiIndex):
            close_col = ('Close', ticker)

        closes = data[close_col].dropna()
        if closes.empty:
            results[ticker] = {'error': 'Empty after dropna'}
            continue

        # Get last 5 close values
        last5 = closes.iloc[-5:].values.flatten()
        latest_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else None

        # Daily change
        daily_chg = None
        if prev_close is not None and prev_close != 0:
            daily_chg = round(latest_close - prev_close, 4)

        # 5-day trend
        trend_5d = None
        if len(last5) >= 2:
            if last5[-1] > last5[0]:
                trend_5d = "UP"
            elif last5[-1] < last5[0]:
                trend_5d = "DOWN"
            else:
                trend_5d = "FLAT"

        results[ticker] = {
            'latest': round(latest_close, 4),
            'daily_chg': daily_chg,
            'trend_5d': trend_5d,
            'last5': [round(x, 4) for x in last5.tolist()],
            'n': len(closes),
            'dates': [str(d.date()) for d in closes.index[-5:].tolist()],
        }
    except Exception as e:
        results[ticker] = {'error': str(e)}

print(json.dumps(results, indent=2))
