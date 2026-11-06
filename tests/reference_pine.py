"""Independent, deliberately differently-structured transcription of the Flux Charts Pine script.

Written directly from the Pine source (not from po3_strategy.py): whole-series indicator arrays, Pine field
names, `na` as None, a PO3List like Pine's `var PO3[] PO3List`, and the two code blocks of the script in order
(entry/state block, then 'Entry Taken' block, then the render region's state flip). It is used ONLY by tests to
cross-check the production engine on identical OHLC data. It is NOT TradingView output.
"""
import math

NA = None


def gt(a, b):  # Pine: comparisons with na are false
    return a is not None and b is not None and not (isinstance(a, float) and math.isnan(a)) and a > b


def le(a, b):
    return a is not None and b is not None and a <= b


def run(bars, algorithmMode="Small Manipulation", breakoutMethod="Wick", tpslMethod="Dynamic", riskAmount="Normal",
        tpPercent=0.3, slPercent=0.4, RR=0.86, atrLen=50, maxDistanceToLastBar=4800, accumulationExpandMult=0.5):
    """bars: list of (time, open, high, low, close). Returns list of trade dicts (PO3s with an entry)."""
    accumulationLength = 40 if algorithmMode == "Small Manipulation" else 11
    accumulationATRMult = 2 if algorithmMode == "Short Accumulation" else 5
    manipulationATRMult = 0.6 if algorithmMode == "Small Manipulation" else 1
    slATRMult = {"Highest": 9.5, "High": 6, "Normal": 5, "Low": 4, "Lowest": 1.5}[riskAmount]
    n = len(bars)
    H = [b[2] for b in bars]; L = [b[3] for b in bars]; C = [b[4] for b in bars]; T = [b[0] for b in bars]

    # ta.tr(true), ta.rma-based ta.atr
    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)]
    atr = [NA] * n
    for i in range(n):
        if i + 1 < atrLen:
            continue
        atr[i] = sum(tr[i + 1 - atrLen:i + 1]) / atrLen if atr[i - 1] is NA else (tr[i] + (atrLen - 1) * atr[i - 1]) / atrLen
    highestAcc = [max(H[i + 1 - accumulationLength:i + 1]) if i + 1 >= accumulationLength else NA for i in range(n)]
    lowestAcc = [min(L[i + 1 - accumulationLength:i + 1]) if i + 1 >= accumulationLength else NA for i in range(n)]

    PO3List, lastPO3 = [], None
    last_bar_index = n - 1
    for bar_index in range(n):
        if not (bar_index > last_bar_index - maxDistanceToLastBar):
            continue
        lowBreakout = L[bar_index] if breakoutMethod == "Wick" else C[bar_index]
        highBreakout = H[bar_index] if breakoutMethod == "Wick" else C[bar_index]
        a = atr[bar_index]

        createNewPO3 = True
        if lastPO3 is not None and lastPO3["exitPrice"] is NA:
            createNewPO3 = False
        if createNewPO3:
            lastPO3 = dict(state="Waiting For Accumulation", startTime=T[bar_index], exitPrice=NA, entryPrice=NA,
                           accumulationEndIndex=NA, accumulationTop=NA, accumulationBottom=NA, accumulationEndTop=NA,
                           accumulationEndBottom=NA, manipulationDirection=NA, entryType=NA, entryTime=NA, exitTime=NA,
                           slTarget=NA, tpTarget=NA)
            PO3List.insert(0, lastPO3)

        p = lastPO3
        if p["state"] == "Waiting For Accumulation":
            if le(None if highestAcc[bar_index] is NA or a is NA else highestAcc[bar_index] - lowestAcc[bar_index],
                  None if a is NA else a * accumulationATRMult):
                p["state"] = "Waiting For Accumulation End"
                p["accumulationEndIndex"] = bar_index
                p["accumulationTop"] = highestAcc[bar_index] + a * accumulationExpandMult
                p["accumulationBottom"] = lowestAcc[bar_index] - a * accumulationExpandMult
        if p["state"] == "Waiting For Accumulation End":
            if highBreakout > p["accumulationTop"] or lowBreakout < p["accumulationBottom"]:
                p["state"] = "Waiting For Manipulation"
                p["accumulationEndTop"] = highBreakout
                p["accumulationEndBottom"] = lowBreakout
            else:
                p["accumulationEndIndex"] = bar_index
        if p["state"] == "Waiting For Manipulation":
            if bar_index > p["accumulationEndIndex"]:
                if highBreakout > p["accumulationTop"] + a * manipulationATRMult:
                    p["state"] = "Waiting For Distribution"; p["manipulationDirection"] = "Bullish"
                elif lowBreakout < p["accumulationBottom"] - a * manipulationATRMult:
                    p["state"] = "Waiting For Distribution"; p["manipulationDirection"] = "Bearish"
        if p["state"] == "Waiting For Distribution":
            p["state"] = "Entry Taken"
            p["entryTime"] = T[bar_index]
            p["entryPrice"] = C[bar_index]
            sign = 1 if p["manipulationDirection"] == "Bearish" else -1      # +1 long, -1 short
            p["entryType"] = "Long" if sign == 1 else "Short"
            if tpslMethod == "Fixed":
                p["slTarget"] = p["entryPrice"] * (1 - sign * slPercent / 100.0)
                p["tpTarget"] = p["entryPrice"] * (1 + sign * tpPercent / 100.0)
            else:
                p["slTarget"] = p["entryPrice"] - sign * a * slATRMult
                p["tpTarget"] = p["entryPrice"] + sign * abs(p["entryPrice"] - p["slTarget"]) * RR

        if p["state"] == "Entry Taken" and T[bar_index] > p["entryTime"]:
            h, l, e = H[bar_index], L[bar_index], p["entryPrice"]
            def hit(state, price):
                p["exitPrice"], p["exitTime"], p["state"] = price, T[bar_index], state
            if tpslMethod == "Fixed":
                if p["entryType"] == "Long" and ((h / e) - 1) * 100 >= tpPercent: hit("Take Profit", e * (1 + tpPercent / 100.0))
                if p["entryType"] == "Short" and ((l / e) - 1) * 100 <= -tpPercent: hit("Take Profit", e * (1 - tpPercent / 100.0))
                if p["entryType"] == "Long" and ((l / e) - 1) * 100 <= -slPercent: hit("Stop Loss", e * (1 - slPercent / 100.0))
                if p["entryType"] == "Short" and ((h / e) - 1) * 100 >= slPercent: hit("Stop Loss", e * (1 + slPercent / 100.0))
            else:
                if p["entryType"] == "Long" and h >= p["tpTarget"]: hit("Take Profit", p["tpTarget"])
                if p["entryType"] == "Short" and l <= p["tpTarget"]: hit("Take Profit", p["tpTarget"])
                if p["entryType"] == "Long" and l <= p["slTarget"]: hit("Stop Loss", p["slTarget"])
                if p["entryType"] == "Short" and h >= p["slTarget"]: hit("Stop Loss", p["slTarget"])
        # render region
        if p["state"] == "Stop Loss" and T[bar_index] >= p["exitTime"]:
            p["state"] = "Done"
        if p["state"] == "Take Profit":
            p["state"] = "Done"

    trades = [q for q in reversed(PO3List) if q["entryPrice"] is not NA]
    return [dict(type=q["entryType"], ts=q["entryTime"], entry=q["entryPrice"], sl=q["slTarget"], tp=q["tpTarget"],
                 outcome={"Done": None}.get(q["state"], None), exit_ts=q["exitTime"], exit_price=q["exitPrice"])
            for q in trades]
