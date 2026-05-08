//+------------------------------------------------------------------+
//| MT5BridgeFile.mq5                                                |
//|                                                                  |
//| File-based bridge for mt5_quant_trader. NO DLLs, NO LIBRARIES.   |
//| Just MT5's built-in File* APIs reading/writing JSON in           |
//| MQL5/Files/mt5qt/ which Python (running natively on macOS)       |
//| reads/writes too. Works inside Wine without networking.          |
//|                                                                  |
//| Protocol:                                                        |
//|   Python writes a request to: MQL5/Files/mt5qt/req/<id>.json     |
//|   EA polls req/ every 100ms, processes the request,              |
//|   writes reply to:           MQL5/Files/mt5qt/rep/<id>.json      |
//|   Python reads + deletes both files.                             |
//|                                                                  |
//| Methods (Phase 1 scope):                                         |
//|   ping, account_info, symbols_get, symbol_info, copy_rates       |
//|   Phase 7:   order_send, position_close                          |
//|   Phase 2.5: positions_get, history_deals_get  (v1.1)            |
//+------------------------------------------------------------------+
#property copyright "mt5_quant_trader"
#property version   "1.1"
#property strict

input int Poll_Ms     = 100;
input bool Verbose_Log = true;

string REQ_DIR = "mt5qt\\req";
string REP_DIR = "mt5qt\\rep";

int OnInit() {
   FolderCreate(REQ_DIR, FILE_COMMON);
   FolderCreate(REP_DIR, FILE_COMMON);
   FolderCreate(REQ_DIR);
   FolderCreate(REP_DIR);
   EventSetMillisecondTimer(Poll_Ms);
   Print("MT5BridgeFile: ready. Watching MQL5/Files/mt5qt/req/");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
   EventKillTimer();
   Print("MT5BridgeFile: stopped");
}

void OnTimer() {
   string req_pattern = REQ_DIR + "\\*.json";
   string fname;
   long search = FileFindFirst(req_pattern, fname);
   if (search == INVALID_HANDLE) return;
   do {
      string full_req = REQ_DIR + "\\" + fname;
      string body = read_all(full_req);
      if (StringLen(body) > 0) {
         string id = jstr(body, "id");
         string method = jstr(body, "method");
         string reply;
         if (StringLen(method) == 0) {
            reply = ko(id, "missing method");
         } else if (method == "ping") {
            reply = ok(id, "{\"pong\":true,\"build\":" + IntegerToString(__MQL5BUILD__) + "}");
         } else if (method == "account_info") {
            reply = ok(id, json_account());
         } else if (method == "symbols_get") {
            reply = ok(id, json_symbols());
         } else if (method == "symbol_info") {
            reply = ok(id, json_symbol_info(jstr(body, "params.name")));
         } else if (method == "copy_rates") {
            reply = ok(id, json_copy_rates(
               jstr(body, "params.name"),
               jstr(body, "params.timeframe"),
               (int)StringToInteger(jstr(body, "params.count"))));
         } else if (method == "order_send") {
            reply = ok(id, do_order_send(
               jstr(body, "params.symbol"),
               jstr(body, "params.direction"),
               StringToDouble(jstr(body, "params.lots")),
               StringToDouble(jstr(body, "params.sl")),
               StringToDouble(jstr(body, "params.tp")),
               (int)StringToInteger(jstr(body, "params.deviation")),
               jstr(body, "params.comment")));
         } else if (method == "position_close") {
            reply = ok(id, do_position_close(
               (long)StringToInteger(jstr(body, "params.ticket")),
               jstr(body, "params.comment")));
         } else if (method == "positions_get") {
            reply = ok(id, json_positions());
         } else if (method == "history_deals_get") {
            reply = ok(id, json_history_deals(
               jstr(body, "params.since_utc"),
               jstr(body, "params.until_utc")));
         } else {
            reply = ko(id, "unknown method: " + method);
         }
         // write reply, then delete request
         string full_rep = REP_DIR + "\\" + fname;
         write_all(full_rep, reply);
         FileDelete(full_req);
         if (Verbose_Log) Print("MT5BridgeFile: ", method, " -> ", StringSubstr(reply, 0, 80));
      }
   } while (FileFindNext(search, fname));
   FileFindClose(search);
}

//+------------------------------------------------------------------+
//| File I/O                                                         |
//+------------------------------------------------------------------+
string read_all(string path) {
   int h = FileOpen(path, FILE_READ|FILE_TXT|FILE_ANSI);
   if (h == INVALID_HANDLE) return "";
   string acc = "";
   while (!FileIsEnding(h)) acc += FileReadString(h);
   FileClose(h);
   return acc;
}

void write_all(string path, string data) {
   int h = FileOpen(path, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if (h == INVALID_HANDLE) return;
   FileWriteString(h, data);
   FileClose(h);
}

//+------------------------------------------------------------------+
//| Reply builders                                                   |
//+------------------------------------------------------------------+
string ok(string id, string data_json) {
   return "{\"id\":\"" + esc(id) + "\",\"ok\":true,\"data\":" + data_json + "}";
}
string ko(string id, string err) {
   return "{\"id\":\"" + esc(id) + "\",\"ok\":false,\"error\":\"" + esc(err) + "\"}";
}

//+------------------------------------------------------------------+
//| JSON encoders                                                    |
//+------------------------------------------------------------------+
string json_account() {
   string out = "{";
   out += kv_int("login",    (long)AccountInfoInteger(ACCOUNT_LOGIN));        out += ",";
   out += kv_str("name",     AccountInfoString(ACCOUNT_NAME));                out += ",";
   out += kv_str("server",   AccountInfoString(ACCOUNT_SERVER));              out += ",";
   out += kv_str("currency", AccountInfoString(ACCOUNT_CURRENCY));            out += ",";
   out += kv_int("leverage", (long)AccountInfoInteger(ACCOUNT_LEVERAGE));     out += ",";
   out += kv_dbl("balance",  AccountInfoDouble(ACCOUNT_BALANCE));             out += ",";
   out += kv_dbl("equity",   AccountInfoDouble(ACCOUNT_EQUITY));              out += ",";
   out += kv_dbl("margin",   AccountInfoDouble(ACCOUNT_MARGIN));              out += ",";
   out += kv_dbl("margin_free", AccountInfoDouble(ACCOUNT_MARGIN_FREE));
   out += "}";
   return out;
}

string json_symbols() {
   int total = SymbolsTotal(true);
   string parts = "";
   for (int i = 0; i < total; ++i) {
      if (i > 0) parts += ",";
      parts += "\"" + esc(SymbolName(i, true)) + "\"";
   }
   return "[" + parts + "]";
}

string json_symbol_info(string name) {
   if (StringLen(name) == 0) return "null";
   if (!SymbolInfoInteger(name, SYMBOL_VISIBLE)) SymbolSelect(name, true);
   string out = "{";
   out += kv_str("name",                name);                                                     out += ",";
   out += kv_str("description",         SymbolInfoString(name, SYMBOL_DESCRIPTION));               out += ",";
   out += kv_int("digits",              SymbolInfoInteger(name, SYMBOL_DIGITS));                    out += ",";
   out += kv_dbl("point",               SymbolInfoDouble(name, SYMBOL_POINT));                      out += ",";
   out += kv_dbl("trade_tick_size",     SymbolInfoDouble(name, SYMBOL_TRADE_TICK_SIZE));            out += ",";
   out += kv_dbl("trade_tick_value",    SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE));           out += ",";
   out += kv_dbl("trade_contract_size", SymbolInfoDouble(name, SYMBOL_TRADE_CONTRACT_SIZE));        out += ",";
   out += kv_dbl("volume_min",          SymbolInfoDouble(name, SYMBOL_VOLUME_MIN));                 out += ",";
   out += kv_dbl("volume_max",          SymbolInfoDouble(name, SYMBOL_VOLUME_MAX));                 out += ",";
   out += kv_dbl("volume_step",         SymbolInfoDouble(name, SYMBOL_VOLUME_STEP));                out += ",";
   out += kv_int("spread",              SymbolInfoInteger(name, SYMBOL_SPREAD));                    out += ",";
   out += kv_str("currency_profit",     SymbolInfoString(name, SYMBOL_CURRENCY_PROFIT));            out += ",";
   out += kv_str("currency_margin",     SymbolInfoString(name, SYMBOL_CURRENCY_MARGIN));
   out += "}";
   return out;
}

string json_copy_rates(string name, string tf_name, int count) {
   if (StringLen(name) == 0 || count <= 0) return "[]";
   ENUM_TIMEFRAMES tf = tf_from_string(tf_name);
   MqlRates r[];
   int got = CopyRates(name, tf, 0, count, r);
   if (got <= 0) return "[]";
   string out = "[";
   for (int i = 0; i < got; ++i) {
      if (i > 0) out += ",";
      out += "{";
      out += kv_int("time", (long)r[i].time);    out += ",";
      out += kv_dbl("open", r[i].open);          out += ",";
      out += kv_dbl("high", r[i].high);          out += ",";
      out += kv_dbl("low",  r[i].low);           out += ",";
      out += kv_dbl("close",r[i].close);         out += ",";
      out += kv_int("volume", (long)r[i].tick_volume);
      out += "}";
   }
   out += "]";
   return out;
}

string do_order_send(string symbol, string direction, double lots, double sl, double tp, int deviation, string comment) {
   if (StringLen(symbol) == 0 || lots <= 0)
      return "{\"ok\":false,\"error\":\"bad_params\"}";
   if (!SymbolInfoInteger(symbol, SYMBOL_VISIBLE)) SymbolSelect(symbol, true);
   MqlTick tk;
   if (!SymbolInfoTick(symbol, tk)) return "{\"ok\":false,\"error\":\"no_tick\"}";
   bool is_long = (direction == "LONG");
   double price = is_long ? tk.ask : tk.bid;

   MqlTradeRequest req; ZeroMemory(req);
   MqlTradeResult  res; ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = symbol;
   req.volume       = lots;
   req.type         = is_long ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = price;
   req.sl           = sl;
   req.tp           = (tp > 0 ? tp : 0);
   req.deviation    = deviation;
   req.magic        = 770070;
   req.comment      = comment;
   req.type_time    = ORDER_TIME_GTC;
   req.type_filling = ORDER_FILLING_FOK;

   if (!OrderSend(req, res))
      return StringFormat("{\"ok\":false,\"error\":\"OrderSend_failed\",\"retcode\":%d}", (int)res.retcode);
   if (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL)
      return StringFormat("{\"ok\":false,\"error\":\"retcode\",\"retcode\":%d}", (int)res.retcode);

   string out = "{";
   out += "\"ok\":true,";
   out += kv_int("ticket", (long)res.order);     out += ",";
   out += kv_dbl("fill_price", res.price);       out += ",";
   out += kv_dbl("volume", res.volume);          out += ",";
   out += kv_int("retcode", (long)res.retcode);
   out += "}";
   return out;
}

string do_position_close(long ticket, string comment) {
   if (!PositionSelectByTicket((ulong)ticket))
      return "{\"ok\":false,\"error\":\"no_position\"}";
   string symbol = PositionGetString(POSITION_SYMBOL);
   double volume = PositionGetDouble(POSITION_VOLUME);
   long type     = PositionGetInteger(POSITION_TYPE);
   MqlTick tk;
   if (!SymbolInfoTick(symbol, tk)) return "{\"ok\":false,\"error\":\"no_tick\"}";
   double price = (type == POSITION_TYPE_BUY ? tk.bid : tk.ask);

   MqlTradeRequest req; ZeroMemory(req);
   MqlTradeResult  res; ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.position     = (ulong)ticket;
   req.symbol       = symbol;
   req.volume       = volume;
   req.type         = (type == POSITION_TYPE_BUY ? ORDER_TYPE_SELL : ORDER_TYPE_BUY);
   req.price        = price;
   req.deviation    = 20;
   req.magic        = 770070;
   req.comment      = comment;
   req.type_time    = ORDER_TIME_GTC;
   req.type_filling = ORDER_FILLING_FOK;
   if (!OrderSend(req, res))
      return StringFormat("{\"ok\":false,\"error\":\"close_failed\",\"retcode\":%d}", (int)res.retcode);
   if (res.retcode != TRADE_RETCODE_DONE)
      return StringFormat("{\"ok\":false,\"error\":\"retcode\",\"retcode\":%d}", (int)res.retcode);
   return "{\"ok\":true}";
}

//+------------------------------------------------------------------+
//| Phase 2.5: positions_get — every open position on the account.   |
//+------------------------------------------------------------------+
string json_positions() {
   int total = PositionsTotal();
   string out = "[";
   bool first = true;
   for (int i = 0; i < total; ++i) {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (!PositionSelectByTicket(ticket)) continue;
      if (!first) out += ",";
      first = false;
      out += "{";
      out += kv_int("ticket",        (long)ticket);                                                                         out += ",";
      out += kv_str("symbol",        PositionGetString(POSITION_SYMBOL));                                                   out += ",";
      out += kv_int("type",          (long)PositionGetInteger(POSITION_TYPE));                                              out += ",";
      out += kv_dbl("volume",        PositionGetDouble(POSITION_VOLUME));                                                   out += ",";
      out += kv_dbl("price_open",    PositionGetDouble(POSITION_PRICE_OPEN));                                               out += ",";
      out += kv_dbl("sl",            PositionGetDouble(POSITION_SL));                                                       out += ",";
      out += kv_dbl("tp",            PositionGetDouble(POSITION_TP));                                                       out += ",";
      out += kv_dbl("price_current", PositionGetDouble(POSITION_PRICE_CURRENT));                                            out += ",";
      out += kv_dbl("profit",        PositionGetDouble(POSITION_PROFIT));                                                   out += ",";
      out += kv_dbl("swap",          PositionGetDouble(POSITION_SWAP));                                                     out += ",";
      // MT5 attributes commission to deals, not positions; expose 0.0 for shape parity.
      out += kv_dbl("commission",    0.0);                                                                                  out += ",";
      out += kv_str("time_open_utc", TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE | TIME_SECONDS));  out += ",";
      out += kv_int("magic",         (long)PositionGetInteger(POSITION_MAGIC));                                             out += ",";
      out += kv_str("comment",       PositionGetString(POSITION_COMMENT));
      out += "}";
   }
   out += "]";
   return out;
}

//+------------------------------------------------------------------+
//| Phase 2.5: history_deals_get — closed deals since a UTC          |
//| timestamp. Used by the position-manager reconciliation loop to   |
//| detect manual closes done in MT5.                                |
//+------------------------------------------------------------------+
string json_history_deals(string since_iso, string until_iso) {
   datetime since = (StringLen(since_iso) > 0)
                     ? StringToTime(since_iso) : (TimeCurrent() - 60*60*24*30);
   datetime until = (StringLen(until_iso) > 0)
                     ? StringToTime(until_iso) : TimeCurrent();
   if (!HistorySelect(since, until)) return "[]";
   int total = HistoryDealsTotal();
   string out = "[";
   bool first = true;
   for (int i = 0; i < total; ++i) {
      ulong t = HistoryDealGetTicket(i);
      if (t == 0) continue;
      if (!first) out += ",";
      first = false;
      out += "{";
      out += kv_int("ticket",      (long)t);                                                                          out += ",";
      out += kv_int("order",       (long)HistoryDealGetInteger(t, DEAL_ORDER));                                       out += ",";
      out += kv_int("position_id", (long)HistoryDealGetInteger(t, DEAL_POSITION_ID));                                 out += ",";
      out += kv_str("time_utc",    TimeToString((datetime)HistoryDealGetInteger(t, DEAL_TIME), TIME_DATE | TIME_SECONDS)); out += ",";
      out += kv_int("type",        HistoryDealGetInteger(t, DEAL_TYPE));                                              out += ",";
      out += kv_int("entry",       HistoryDealGetInteger(t, DEAL_ENTRY));                                             out += ",";
      out += kv_str("symbol",      HistoryDealGetString(t, DEAL_SYMBOL));                                             out += ",";
      out += kv_dbl("volume",      HistoryDealGetDouble(t, DEAL_VOLUME));                                             out += ",";
      out += kv_dbl("price",       HistoryDealGetDouble(t, DEAL_PRICE));                                              out += ",";
      out += kv_dbl("profit",      HistoryDealGetDouble(t, DEAL_PROFIT));                                             out += ",";
      out += kv_dbl("swap",        HistoryDealGetDouble(t, DEAL_SWAP));                                               out += ",";
      out += kv_dbl("commission",  HistoryDealGetDouble(t, DEAL_COMMISSION));                                         out += ",";
      out += kv_str("comment",     HistoryDealGetString(t, DEAL_COMMENT));
      out += "}";
   }
   out += "]";
   return out;
}

ENUM_TIMEFRAMES tf_from_string(string s) {
   if (s == "M1")  return PERIOD_M1;
   if (s == "M5")  return PERIOD_M5;
   if (s == "M15") return PERIOD_M15;
   if (s == "M30") return PERIOD_M30;
   if (s == "H1")  return PERIOD_H1;
   if (s == "H4")  return PERIOD_H4;
   if (s == "D1")  return PERIOD_D1;
   if (s == "W1")  return PERIOD_W1;
   if (s == "MN1") return PERIOD_MN1;
   return PERIOD_CURRENT;
}

//+------------------------------------------------------------------+
//| JSON helpers                                                     |
//+------------------------------------------------------------------+
string kv_str(string k, string v) { return "\"" + esc(k) + "\":\"" + esc(v) + "\""; }
string kv_int(string k, long v)   { return "\"" + esc(k) + "\":" + IntegerToString(v); }
string kv_dbl(string k, double v) { return "\"" + esc(k) + "\":" + DoubleToString(v, 8); }

string esc(const string s) {
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\n", "\\n");
   StringReplace(out, "\r", "\\r");
   StringReplace(out, "\t", "\\t");
   return out;
}

string jstr(const string body, const string key) {
   string path = key;
   string scope = body;
   int dot = StringFind(path, ".");
   while (dot >= 0) {
      string head = StringSubstr(path, 0, dot);
      int idx = StringFind(scope, "\"" + head + "\"");
      if (idx < 0) return "";
      int brace = StringFind(scope, "{", idx);
      int end = brace + 1;
      int depth = 1;
      while (end < StringLen(scope) && depth > 0) {
         ushort ch = StringGetCharacter(scope, end);
         if (ch == '{') depth++;
         else if (ch == '}') depth--;
         end++;
      }
      scope = StringSubstr(scope, brace, end - brace);
      path = StringSubstr(path, dot + 1);
      dot = StringFind(path, ".");
   }
   string look = "\"" + path + "\"";
   int p = StringFind(scope, look);
   if (p < 0) return "";
   p = StringFind(scope, ":", p);
   if (p < 0) return "";
   p++;
   while (p < StringLen(scope) && StringGetCharacter(scope, p) == ' ') p++;
   if (p >= StringLen(scope)) return "";
   ushort first = StringGetCharacter(scope, p);
   if (first == '"') {
      int q = p + 1;
      string acc = "";
      while (q < StringLen(scope)) {
         ushort c = StringGetCharacter(scope, q);
         if (c == '\\' && q + 1 < StringLen(scope)) { acc += ShortToString(StringGetCharacter(scope, q + 1)); q += 2; continue; }
         if (c == '"') break;
         acc += ShortToString(c);
         q++;
      }
      return acc;
   }
   int q = p;
   while (q < StringLen(scope)) {
      ushort c = StringGetCharacter(scope, q);
      if (c == ',' || c == '}' || c == ' ' || c == '\n' || c == '\r' || c == '\t') break;
      q++;
   }
   return StringSubstr(scope, p, q - p);
}
