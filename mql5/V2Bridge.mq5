//+------------------------------------------------------------------+
//|                                                  V2Bridge.mq5    |
//|                            mt5_quant_trader_v2 file-bridge EA    |
//|                                                                  |
//|  Drop on any chart in MetaTrader 5 with auto-trading + DLL      |
//|  imports both enabled. The EA reads JSON requests written by    |
//|  Python into MQL5/Files/v2_bridge_request.json and writes the   |
//|  response to MQL5/Files/v2_bridge_response.json.                |
//|                                                                  |
//|  Implements RPC methods used by the Python side:                |
//|    - account_info        — login, balance, equity, server, ...  |
//|    - symbol_info         — tick_size, tick_value, contract sz   |
//|    - copy_rates          — OHLCV bars for (symbol, timeframe)   |
//|    - positions_get       — every open position                  |
//|    - position_close      — close one position by ticket         |
//|    - history_deals_get   — closed deals since a UTC timestamp   |
//|                                                                  |
//|  License: MIT (see repo LICENSE).                               |
//+------------------------------------------------------------------+
#property copyright "mt5_quant_trader_v2 contributors"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input string  RequestFile  = "v2_bridge_request.json";
input string  ResponseFile = "v2_bridge_response.json";
input int     PollMs       = 100;       // Polling interval
input int     MaxBars      = 8000;      // Hard cap on copy_rates count
input int     MagicNumber  = 50310190;  // Magic for our trades

CTrade trade;

//+------------------------------------------------------------------+
//| OnInit / OnDeinit                                                |
//+------------------------------------------------------------------+
int OnInit()
{
    EventSetMillisecondTimer(PollMs);
    Print("V2Bridge EA started. Polling ", RequestFile, " every ", PollMs, " ms");
    return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
    EventKillTimer();
}

//+------------------------------------------------------------------+
//| Polling loop                                                     |
//+------------------------------------------------------------------+
void OnTimer()
{
    string req_path = RequestFile;
    if(!FileIsExist(req_path, FILE_COMMON)) return;

    int fh = FileOpen(req_path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
    if(fh == INVALID_HANDLE) return;
    string body = "";
    while(!FileIsEnding(fh)) body += FileReadString(fh) + "\n";
    FileClose(fh);
    FileDelete(req_path, FILE_COMMON);

    if(StringLen(body) == 0) return;

    string method = JsonString(body, "method");
    string id     = JsonString(body, "id");
    string params = JsonObject(body, "params");

    string response;
    if(method == "account_info")        response = HandleAccountInfo();
    else if(method == "symbol_info")    response = HandleSymbolInfo(params);
    else if(method == "copy_rates")     response = HandleCopyRates(params);
    else if(method == "positions_get")  response = HandlePositionsGet();
    else if(method == "position_close") response = HandlePositionClose(params);
    else if(method == "history_deals_get") response = HandleHistoryDealsGet(params);
    else                                response = ErrorJson("unknown method: " + method);

    string resp_full = "{\"id\":\"" + id + "\",\"ok\":true,\"data\":" + response + "}";
    int oh = FileOpen(ResponseFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
    if(oh != INVALID_HANDLE)
    {
        FileWriteString(oh, resp_full);
        FileClose(oh);
    }
}

//+------------------------------------------------------------------+
//| RPC: account_info                                                |
//+------------------------------------------------------------------+
string HandleAccountInfo()
{
    string j = "{";
    j += "\"login\":"      + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
    j += "\"balance\":"    + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
    j += "\"equity\":"     + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
    j += "\"margin\":"     + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",";
    j += "\"margin_free\":"+ DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ",";
    j += "\"margin_level\":"+ DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2) + ",";
    j += "\"currency\":\"" + AccountInfoString(ACCOUNT_CURRENCY) + "\",";
    j += "\"leverage\":"   + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
    j += "\"name\":\""     + EscapeJson(AccountInfoString(ACCOUNT_NAME)) + "\",";
    j += "\"server\":\""   + EscapeJson(AccountInfoString(ACCOUNT_SERVER)) + "\",";
    j += "\"company\":\""  + EscapeJson(AccountInfoString(ACCOUNT_COMPANY)) + "\",";
    j += "\"trade_mode\":" + IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_MODE));
    j += "}";
    return j;
}

//+------------------------------------------------------------------+
//| RPC: symbol_info                                                 |
//+------------------------------------------------------------------+
string HandleSymbolInfo(string params)
{
    string name = JsonString(params, "name");
    if(!SymbolSelect(name, true)) return ErrorJson("symbol_select failed: " + name);

    string j = "{";
    j += "\"name\":\""        + name + "\",";
    j += "\"tick_size\":"     + DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_SIZE), 8) + ",";
    j += "\"tick_value\":"    + DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE), 8) + ",";
    j += "\"point\":"         + DoubleToString(SymbolInfoDouble(name, SYMBOL_POINT), 8) + ",";
    j += "\"digits\":"        + IntegerToString(SymbolInfoInteger(name, SYMBOL_DIGITS)) + ",";
    j += "\"volume_step\":"   + DoubleToString(SymbolInfoDouble(name, SYMBOL_VOLUME_STEP), 4) + ",";
    j += "\"volume_min\":"    + DoubleToString(SymbolInfoDouble(name, SYMBOL_VOLUME_MIN), 4) + ",";
    j += "\"volume_max\":"    + DoubleToString(SymbolInfoDouble(name, SYMBOL_VOLUME_MAX), 4) + ",";
    j += "\"contract_size\":" + DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_CONTRACT_SIZE), 4);
    j += "}";
    return j;
}

//+------------------------------------------------------------------+
//| RPC: copy_rates                                                  |
//+------------------------------------------------------------------+
string HandleCopyRates(string params)
{
    string sym  = JsonString(params, "name");
    string tf_s = JsonString(params, "timeframe");
    int    n    = (int)StringToInteger(JsonString(params, "count"));
    n = MathMin(n, MaxBars);
    ENUM_TIMEFRAMES tf = TimeframeFromString(tf_s);

    MqlRates rates[];
    int got = CopyRates(sym, tf, 0, n, rates);
    if(got <= 0) return ErrorJson("copy_rates returned " + IntegerToString(got));

    string j = "[";
    for(int i = 0; i < got; i++)
    {
        if(i > 0) j += ",";
        j += "{\"time\":" + IntegerToString((long)rates[i].time) + ",";
        j += "\"open\":"  + DoubleToString(rates[i].open, 8) + ",";
        j += "\"high\":"  + DoubleToString(rates[i].high, 8) + ",";
        j += "\"low\":"   + DoubleToString(rates[i].low, 8) + ",";
        j += "\"close\":" + DoubleToString(rates[i].close, 8) + ",";
        j += "\"tick_volume\":" + IntegerToString((long)rates[i].tick_volume) + "}";
    }
    j += "]";
    return j;
}

//+------------------------------------------------------------------+
//| RPC: positions_get  — every open position                        |
//+------------------------------------------------------------------+
string HandlePositionsGet()
{
    int total = PositionsTotal();
    string j = "[";
    for(int i = 0; i < total; i++)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(!PositionSelectByTicket(ticket)) continue;

        if(j != "[") j += ",";
        j += "{\"ticket\":"   + IntegerToString((long)ticket) + ",";
        j += "\"symbol\":\""  + PositionGetString(POSITION_SYMBOL) + "\",";
        j += "\"type\":"      + IntegerToString(PositionGetInteger(POSITION_TYPE)) + ",";
        j += "\"volume\":"    + DoubleToString(PositionGetDouble(POSITION_VOLUME), 4) + ",";
        j += "\"price_open\":"+ DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 8) + ",";
        j += "\"sl\":"        + DoubleToString(PositionGetDouble(POSITION_SL), 8) + ",";
        j += "\"tp\":"        + DoubleToString(PositionGetDouble(POSITION_TP), 8) + ",";
        j += "\"price_current\":"+ DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), 8) + ",";
        j += "\"profit\":"    + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + ",";
        j += "\"swap\":"      + DoubleToString(PositionGetDouble(POSITION_SWAP), 2) + ",";
        // commission: MT5 attributes commission to deal, not position;
        // expose 0.0 so the Python side has a consistent shape.
        j += "\"commission\":0.0,";
        j += "\"time_open_utc\":\"" + TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE | TIME_SECONDS) + "\",";
        j += "\"magic\":"     + IntegerToString((long)PositionGetInteger(POSITION_MAGIC)) + ",";
        j += "\"comment\":\"" + EscapeJson(PositionGetString(POSITION_COMMENT)) + "\"";
        j += "}";
    }
    j += "]";
    return j;
}

//+------------------------------------------------------------------+
//| RPC: position_close                                              |
//+------------------------------------------------------------------+
string HandlePositionClose(string params)
{
    long ticket = (long)StringToInteger(JsonString(params, "ticket"));
    int  dev    = (int)StringToInteger(JsonString(params, "deviation"));
    if(dev <= 0) dev = 20;
    trade.SetDeviationInPoints(dev);

    if(!PositionSelectByTicket(ticket))
        return ErrorJson("ticket not found: " + IntegerToString(ticket));

    if(!trade.PositionClose(ticket))
        return ErrorJson("PositionClose failed retcode=" + IntegerToString(trade.ResultRetcode())
                          + " comment=" + trade.ResultComment());

    string j = "{";
    j += "\"ok\":true,";
    j += "\"ticket\":" + IntegerToString(ticket) + ",";
    j += "\"retcode\":"+ IntegerToString(trade.ResultRetcode()) + ",";
    j += "\"deal\":"   + IntegerToString((long)trade.ResultDeal()) + ",";
    j += "\"price\":"  + DoubleToString(trade.ResultPrice(), 8) + ",";
    j += "\"comment\":\"" + EscapeJson(trade.ResultComment()) + "\"";
    j += "}";
    return j;
}

//+------------------------------------------------------------------+
//| RPC: history_deals_get                                           |
//|     params.since_utc = ISO-8601 string                           |
//+------------------------------------------------------------------+
string HandleHistoryDealsGet(string params)
{
    string since_iso = JsonString(params, "since_utc");
    string until_iso = JsonString(params, "until_utc");
    datetime since = (StringLen(since_iso) > 0)
                      ? StringToTime(since_iso) : (TimeCurrent() - 60*60*24*30);
    datetime until = (StringLen(until_iso) > 0)
                      ? StringToTime(until_iso) : TimeCurrent();

    if(!HistorySelect(since, until)) return ErrorJson("HistorySelect failed");

    int total = HistoryDealsTotal();
    string j = "[";
    bool first = true;
    for(int i = 0; i < total; i++)
    {
        ulong t = HistoryDealGetTicket(i);
        if(t == 0) continue;
        if(!first) j += ",";
        first = false;
        j += "{\"ticket\":"   + IntegerToString((long)t) + ",";
        j += "\"order\":"     + IntegerToString((long)HistoryDealGetInteger(t, DEAL_ORDER)) + ",";
        j += "\"position_id\":"+ IntegerToString((long)HistoryDealGetInteger(t, DEAL_POSITION_ID)) + ",";
        j += "\"time_utc\":\""+ TimeToString((datetime)HistoryDealGetInteger(t, DEAL_TIME), TIME_DATE | TIME_SECONDS) + "\",";
        j += "\"type\":"      + IntegerToString(HistoryDealGetInteger(t, DEAL_TYPE)) + ",";
        j += "\"entry\":"     + IntegerToString(HistoryDealGetInteger(t, DEAL_ENTRY)) + ",";
        j += "\"symbol\":\""  + HistoryDealGetString(t, DEAL_SYMBOL) + "\",";
        j += "\"volume\":"    + DoubleToString(HistoryDealGetDouble(t, DEAL_VOLUME), 4) + ",";
        j += "\"price\":"     + DoubleToString(HistoryDealGetDouble(t, DEAL_PRICE), 8) + ",";
        j += "\"profit\":"    + DoubleToString(HistoryDealGetDouble(t, DEAL_PROFIT), 2) + ",";
        j += "\"swap\":"      + DoubleToString(HistoryDealGetDouble(t, DEAL_SWAP), 2) + ",";
        j += "\"commission\":"+ DoubleToString(HistoryDealGetDouble(t, DEAL_COMMISSION), 2) + ",";
        j += "\"comment\":\"" + EscapeJson(HistoryDealGetString(t, DEAL_COMMENT)) + "\"";
        j += "}";
    }
    j += "]";
    return j;
}

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES TimeframeFromString(const string s)
{
    if(s == "M1")  return PERIOD_M1;
    if(s == "M5")  return PERIOD_M5;
    if(s == "M15") return PERIOD_M15;
    if(s == "M30") return PERIOD_M30;
    if(s == "H1")  return PERIOD_H1;
    if(s == "H4")  return PERIOD_H4;
    if(s == "D1")  return PERIOD_D1;
    if(s == "W1")  return PERIOD_W1;
    if(s == "MN1") return PERIOD_MN1;
    return PERIOD_H1;
}

string ErrorJson(const string msg)
{
    return "{\"ok\":false,\"error\":\"" + EscapeJson(msg) + "\"}";
}

string EscapeJson(const string s)
{
    string out = s;
    StringReplace(out, "\\", "\\\\");
    StringReplace(out, "\"", "\\\"");
    StringReplace(out, "\n", "\\n");
    return out;
}

// --- Tiny JSON helpers — not a full parser, just enough for this RPC. ----
// JsonString: extract string-or-numeric field by key (returns "" if absent).
string JsonString(const string body, const string key)
{
    string needle = "\"" + key + "\"";
    int i = StringFind(body, needle);
    if(i < 0) return "";
    int colon = StringFind(body, ":", i + StringLen(needle));
    if(colon < 0) return "";
    int p = colon + 1;
    int n = StringLen(body);
    while(p < n && (StringGetCharacter(body, p) == ' '
                     || StringGetCharacter(body, p) == '\t')) p++;
    if(p >= n) return "";
    ushort ch = StringGetCharacter(body, p);
    if(ch == '"')
    {
        int end = StringFind(body, "\"", p + 1);
        if(end < 0) return "";
        return StringSubstr(body, p + 1, end - p - 1);
    }
    int end = p;
    while(end < n)
    {
        ushort c = StringGetCharacter(body, end);
        if(c == ',' || c == '}' || c == ']') break;
        end++;
    }
    string raw = StringSubstr(body, p, end - p);
    StringTrimRight(raw); StringTrimLeft(raw);
    return raw;
}

// JsonObject: extract a nested object/array as a raw substring.
string JsonObject(const string body, const string key)
{
    string needle = "\"" + key + "\"";
    int i = StringFind(body, needle);
    if(i < 0) return "";
    int colon = StringFind(body, ":", i + StringLen(needle));
    if(colon < 0) return "";
    int p = colon + 1;
    int n = StringLen(body);
    while(p < n && (StringGetCharacter(body, p) == ' '
                     || StringGetCharacter(body, p) == '\t')) p++;
    if(p >= n) return "";
    ushort opening = StringGetCharacter(body, p);
    if(opening != '{' && opening != '[') return "";
    ushort closing = (opening == '{') ? '}' : ']';
    int depth = 1;
    int end = p + 1;
    while(end < n && depth > 0)
    {
        ushort c = StringGetCharacter(body, end);
        if(c == opening) depth++;
        else if(c == closing) depth--;
        end++;
    }
    return StringSubstr(body, p, end - p);
}
//+------------------------------------------------------------------+
