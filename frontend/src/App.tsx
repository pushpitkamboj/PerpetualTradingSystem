import { useCallback, useEffect, useMemo, useState, type CSSProperties, type FormEvent } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");
const PAGE_WS_ORIGIN = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
const WS_BASE = (import.meta.env.VITE_WS_BASE_URL || `${PAGE_WS_ORIGIN}/api`).replace(/\/$/, "");
const MARKETS = ["BTC-PERP", "ETH-PERP"] as const;
type Market = (typeof MARKETS)[number];
type Side = "long" | "short";
type Session = { token: string; user_id: string; username: string };
type Level = { price: number; qty: number };
type Position = { market: Market; qty: number; entry_price: number; margin: number; liquidation_price: number; unrealized_pnl: number; realized_pnl?: number | null; side?: Side; leverage?: number };
type Fill = { fill_id: string; market: Market; price: number; qty: number; timestamp_ms: number; side?: Side };
type OrderEvent = { order_id: string; market: Market; side?: Side; status: string; price: number; remaining_qty: number; fills: Fill[]; at: number };
type PriceSource = "waiting" | "binance_futures" | "binance_spot" | "local_simulation";
type MarketState = { price: number | null; source: PriceSource; bids: Level[]; asks: Level[]; history: number[] };
type Event = { event_type: string; payload: Record<string, any> };

const emptyMarket = (): MarketState => ({ price: null, source: "waiting", bids: [], asks: [], history: [] });
const money = (n: number | null | undefined, digits = 2) => n == null || !Number.isFinite(n) ? "—" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const quantity = (n: number | null | undefined) => n == null || !Number.isFinite(n) ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: 4 });

async function request(path: string, options: RequestInit = {}, token?: string) {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

export default function App() {
  const [session, setSession] = useState<Session | null>(() => {
    try { return JSON.parse(localStorage.getItem("python-perps-session") || "null"); }
    catch { return null; }
  });
  const [market, setMarket] = useState<Market>("BTC-PERP");
  const [markets, setMarkets] = useState<Record<Market, MarketState>>({ "BTC-PERP": emptyMarket(), "ETH-PERP": emptyMarket() });
  const [marketStatus, setMarketStatus] = useState("connecting");
  const [privateStatus, setPrivateStatus] = useState("signed out");
  const [balance, setBalance] = useState({ available: 0, locked: 0, wallet: 0 });
  const [positions, setPositions] = useState<Partial<Record<Market, Position>>>({});
  const [fills, setFills] = useState<Fill[]>(() => {
    try {
      const savedSession = JSON.parse(localStorage.getItem("python-perps-session") || "null") as Session | null;
      return savedSession?.user_id ? JSON.parse(localStorage.getItem(`python-perps-fills:${savedSession.user_id}`) || "[]") : [];
    } catch { return []; }
  });
  const [orders, setOrders] = useState<OrderEvent[]>([]);
  const [authMode, setAuthMode] = useState<"signup" | "signin">("signup");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [side, setSide] = useState<Side>("long");
  const [manualPrice, setManualPrice] = useState(false);
  const [qty, setQty] = useState("0.001");
  const [price, setPrice] = useState("");
  const [leverage, setLeverage] = useState("5");
  const [fundAmount, setFundAmount] = useState("1000");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const state = markets[market];

  useEffect(() => {
    if (session?.user_id) localStorage.setItem(`python-perps-fills:${session.user_id}`, JSON.stringify(fills));
  }, [fills, session?.user_id]);

  const applyPrivateEvent = useCallback((event: Event) => {
    const p = event.payload || {};
    if (event.event_type === "balance_update") {
      setBalance(prev => ({ available: Number(p.balance ?? 0), locked: Number(p.locked ?? 0), wallet: Number(p.wallet_balance ?? prev.wallet) }));
      if (p.message && p.message !== "account snapshot") setNotice(`${p.message}${p.status === "rejected" ? " — rejected" : ""}`);
    } else if (event.event_type === "create_position") {
      const orderMarket = p.market as Market;
      if (p.position) setPositions(prev => ({ ...prev, [orderMarket]: p.position as Position }));
      else setPositions(prev => { const next = { ...prev }; delete next[orderMarket]; return next; });
      setBalance(prev => ({ ...prev, available: Number(p.available ?? prev.available), locked: Number(p.locked ?? prev.locked) }));
      const nextFills = ((p.fills || []) as Fill[]).map((f: Fill) => ({ ...f, side: p.side as Side | undefined }));
      if (nextFills.length) setFills(prev => [...nextFills.filter(f => !prev.some(old => old.fill_id === f.fill_id)), ...prev].slice(0, 20));
      if (p.order_id && p.order_id !== "account-snapshot" && (p.side === "long" || p.side === "short")) {
        setOrders(prev => [{ order_id: p.order_id, market: orderMarket, side: p.side, status: p.status, price: Number(p.price || 0), remaining_qty: Number(p.remaining_qty || 0), fills: nextFills, at: Date.now() }, ...prev.filter(o => o.order_id !== p.order_id)].slice(0, 12));
      }
      if (nextFills.length) setNotice(`${nextFills.length} fill${nextFills.length === 1 ? "" : "s"} confirmed on ${orderMarket}`);
    } else if (event.event_type === "unrealized_pnl") {
      setPositions(prev => prev[p.market as Market] ? { ...prev, [p.market as Market]: { ...prev[p.market as Market]!, unrealized_pnl: Number(p.pnl) } } : prev);
    } else if (event.event_type === "close_position" || event.event_type === "liquify_position") {
      setPositions(prev => { const next = { ...prev }; delete next[p.market as Market]; return next; });
      setBalance(prev => ({ ...prev, available: Number(p.available ?? prev.available), locked: Number(p.locked ?? 0) }));
      setNotice(event.event_type === "close_position" ? (p.message || "Position closed") : "Position liquidated");
    }
  }, []);

  useEffect(() => {
    let socket: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout>;
    let stopped = false;
    const connect = () => {
      if (stopped) return;
      setMarketStatus("connecting");
      socket = new WebSocket(`${WS_BASE}/ws/market`);
      socket.onopen = () => {
        setMarketStatus("live");
        for (const item of MARKETS) socket?.send(JSON.stringify({ type: "subscribe_market", market: item }));
      };
      socket.onmessage = e => {
        let event: Event;
        try { event = JSON.parse(e.data); } catch { return; }
        const p = event.payload || {};
        if (event.event_type === "binance_data" && MARKETS.includes(p.market)) {
          const m = p.market as Market; const value = Number(p.price);
          const source = (["binance_futures", "binance_spot", "local_simulation"].includes(p.source) ? p.source : "binance_futures") as PriceSource;
          setMarkets(prev => ({ ...prev, [m]: { ...prev[m], price: value, source, history: [...prev[m].history, value].slice(-48) } }));
        } else if (event.event_type === "update_orderbook" && MARKETS.includes(p.market)) {
          const m = p.market as Market;
          setMarkets(prev => ({ ...prev, [m]: { ...prev[m], bids: p.bids || [], asks: p.asks || [] } }));
        }
      };
      socket.onclose = () => { setMarketStatus("reconnecting"); if (!stopped) retry = setTimeout(connect, 1500); };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { stopped = true; clearTimeout(retry); socket?.close(); };
  }, []);

  useEffect(() => {
    if (!session?.token) { setPrivateStatus("signed out"); return; }
    let socket: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout>;
    let stopped = false;
    const connect = () => {
      if (stopped) return;
      setPrivateStatus("connecting");
      socket = new WebSocket(`${WS_BASE}/ws?token=${encodeURIComponent(session.token)}`);
      socket.onopen = () => setPrivateStatus("live");
      socket.onmessage = e => { try { applyPrivateEvent(JSON.parse(e.data)); } catch { /* Ignore malformed events. */ } };
      socket.onclose = () => { setPrivateStatus("reconnecting"); if (!stopped) retry = setTimeout(connect, 1500); };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { stopped = true; clearTimeout(retry); socket?.close(); };
  }, [session, applyPrivateEvent]);

  useEffect(() => {
    setQty(market === "BTC-PERP" ? "0.001" : "0.02");
    setManualPrice(false);
  }, [market]);

  useEffect(() => {
    if (manualPrice) return;
    const best = side === "long" ? state.asks[0]?.price : state.bids[0]?.price;
    const suggested = best || (state.price ? state.price * (side === "long" ? 1.0005 : 0.9995) : null);
    if (suggested) setPrice(suggested.toFixed(market === "BTC-PERP" ? 2 : 2));
  }, [market, side, manualPrice, state.asks[0]?.price, state.bids[0]?.price, state.price]);

  const accountPosition = positions[market];
  const priceSourceLabel = state.source === "local_simulation"
    ? "LOCAL SIMULATED PRICE · NO EXCHANGE FEED"
    : state.source === "binance_spot"
      ? "BINANCE SPOT INDEX PROXY · PAPER EXECUTION"
      : state.source === "binance_futures"
        ? "BINANCE FUTURES MARK PRICE · PAPER EXECUTION"
        : "WAITING FOR PRICE SOURCE";
  const chartPath = useMemo(() => {
    const points = state.history;
    if (points.length === 0) return "";
    if (points.length === 1) return "M0,20 L100,20";
    const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
    return points.map((point, i) => `${i ? "L" : "M"}${(i / (points.length - 1)) * 100},${36 - ((point - min) / span) * 30}`).join(" ");
  }, [state.history]);

  async function authenticate(e: FormEvent) {
    e.preventDefault(); setError(""); setBusy(true);
    try {
      const result = await request(`/${authMode}`, { method: "POST", body: JSON.stringify({ username, password }) });
      const savedFills = JSON.parse(localStorage.getItem(`python-perps-fills:${result.user_id}`) || "[]");
      setFills(Array.isArray(savedFills) ? savedFills : []);
      localStorage.setItem("python-perps-session", JSON.stringify(result)); setSession(result); setPassword(""); setNotice(`${authMode === "signup" ? "Account created" : "Signed in"} as ${result.username}`);
    } catch (e) { setError(e instanceof Error ? e.message : "Authentication failed"); }
    finally { setBusy(false); }
  }

  async function submitOrder(e: FormEvent) {
    e.preventDefault(); if (!session) return;
    setError(""); setNotice(""); setBusy(true);
    try {
      const result = await request("/order", { method: "POST", body: JSON.stringify({ market, side, order_type: "limit", qty: Number(qty), price: Number(price), leverage: Number(leverage) }) }, session.token);
      setNotice(`Order accepted · ${result.order_id.slice(0, 8)} · awaiting match`);
    } catch (e) { setError(e instanceof Error ? e.message : "Order failed"); }
    finally { setBusy(false); }
  }

  async function fundAccount() {
    if (!session) return;
    setError(""); setBusy(true);
    try { await request("/onramp", { method: "POST", body: JSON.stringify({ amount: Number(fundAmount) }) }, session.token); setNotice("Demo funding accepted · waiting for the balance event"); }
    catch (e) { setError(e instanceof Error ? e.message : "Funding request failed"); }
    finally { setBusy(false); }
  }

  async function withdrawFunds() {
    if (!session) return;
    setError(""); setBusy(true);
    try { await request("/offramp", { method: "POST", body: JSON.stringify({ amount: Number(fundAmount) }) }, session.token); setNotice("Withdrawal accepted · waiting for the balance event"); }
    catch (e) { setError(e instanceof Error ? e.message : "Withdrawal request failed"); }
    finally { setBusy(false); }
  }

  async function closePosition() {
    if (!session) return;
    setError(""); setBusy(true);
    try { await request(`/close-position?market=${market}`, { method: "POST" }, session.token); setNotice("Close request accepted · waiting for execution"); }
    catch (e) { setError(e instanceof Error ? e.message : "Close request failed"); }
    finally { setBusy(false); }
  }

  function signOut() {
    localStorage.removeItem("python-perps-session"); setSession(null); setBalance({ available: 0, locked: 0, wallet: 0 }); setPositions({}); setOrders([]); setFills([]); setNotice("Signed out");
  }

  return <main className="terminal">
    <header className="topbar">
      <a className="brand" href="#top"><span className="brand-mark">P</span><span>PYTHON<span className="gold">PERPS</span><small>LOCAL PAPER EXCHANGE</small></span></a>
      <div className="market-tabs">{MARKETS.map(item => <button key={item} className={market === item ? "market-tab active" : "market-tab"} onClick={() => setMarket(item)}>{item}</button>)}</div>
      <div className="top-right"><span className="net-status"><i className={marketStatus === "live" ? "dot good" : "dot"} />{marketStatus === "live" ? "MARKET LIVE" : marketStatus.toUpperCase()}</span>{session ? <button className="user-chip" onClick={signOut}>{session.username}<span>↗</span></button> : <span className="local-badge">SIMULATED</span>}</div>
    </header>

    <section className="ticker-strip">
      <div><span className="eyebrow">MARKET</span><strong>{market}</strong><span className="contract-pill">PERPETUAL</span></div>
      <div><span className="eyebrow">INDEX PRICE</span><strong className="price-large">{state.price == null ? "Waiting for feed…" : `$${money(state.price)}`}</strong></div>
      <div><span className="eyebrow">24H CHANGE</span><strong className="muted">—</strong></div>
      <div><span className="eyebrow">MARKET SOCKET</span><strong className={marketStatus === "live" ? "green-text" : "amber-text"}>{marketStatus.toUpperCase()}</strong></div>
      <div className="ticker-note">{priceSourceLabel}</div>
    </section>

    {(notice || error) && <div className={error ? "toast error-toast" : "toast"} role={error ? "alert" : "status"}>{error || notice}<button onClick={() => { setError(""); setNotice(""); }} aria-label="Dismiss">×</button></div>}

    <section className="workspace">
      <div className="left-column">
        <section className="panel chart-panel">
          <div className="panel-heading"><div><span className="eyebrow">PRICE TRACKER</span><h2>{market} <span className="live-label"><i className={marketStatus === "live" ? "dot good" : "dot"} />LIVE</span></h2></div><span className="chart-range">STREAM · LAST {state.history.length} TICKS</span></div>
          <div className="chart-wrap">{chartPath ? <><div className="chart-price">${money(state.price)}</div><svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-label="Live index price chart"><defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#c6a45e" stopOpacity=".23"/><stop offset="1" stopColor="#c6a45e" stopOpacity="0"/></linearGradient></defs><path d={`${chartPath} L100,40 L0,40 Z`} fill="url(#fill)"/><path d={chartPath} fill="none" stroke="#d6b46c" strokeWidth=".55" vectorEffect="non-scaling-stroke"/></svg></> : <div className="empty-chart"><span className="pulse-ring"/><span>Waiting for the live index stream</span></div>}</div>
          <div className="chart-footer"><span>Source: {priceSourceLabel}</span><span>Chart uses received price ticks</span></div>
        </section>

        <section className="panel book-panel">
          <div className="panel-heading"><div><span className="eyebrow">LIVE MARKET DEPTH</span><h2>Order book</h2></div><span className="depth-state"><i className={marketStatus === "live" ? "dot good" : "dot"}/>{marketStatus === "live" ? "STREAMING" : "WAITING"}</span></div>
          <div className="book-head"><span>PRICE (USD)</span><span>SIZE</span><span>TOTAL</span></div>
          <div className="depth asks">{state.asks.length ? state.asks.slice(0, 7).reverse().map((row, i) => <div className="book-row" key={`a-${i}`}><span>{money(row.price)}</span><span>{quantity(row.qty)}</span><span className="depth-bar" style={{ "--bar": `${Math.min(100, 10 + row.qty * 180)}%` } as CSSProperties}>{quantity(row.qty)}</span></div>) : <div className="empty-row">No asks yet</div>}</div>
          <div className="mid-price"><span>INDEX</span><strong>{state.price == null ? "—" : `$${money(state.price)}`}</strong><span className="spread">Spread {state.asks[0] && state.bids[0] ? `$${money(state.asks[0].price - state.bids[0].price)}` : "—"}</span></div>
          <div className="depth bids">{state.bids.length ? state.bids.slice(0, 7).map((row, i) => <div className="book-row" key={`b-${i}`}><span>{money(row.price)}</span><span>{quantity(row.qty)}</span><span className="depth-bar" style={{ "--bar": `${Math.min(100, 10 + row.qty * 180)}%` } as CSSProperties}>{quantity(row.qty)}</span></div>) : <div className="empty-row">No bids yet</div>}</div>
          <div className="book-foot"><span><i className="dot red-dot"/>ASKS</span><span><i className="dot good"/>BIDS</span><span>Updates arrive over WebSocket</span></div>
        </section>
      </div>

      <aside className="right-column">
        {!session ? <section className="panel auth-panel">
          <div className="panel-heading"><div><span className="eyebrow">YOUR ACCOUNT</span><h2>Start trading</h2></div><span className="lock-icon">⌑</span></div>
          <p className="subtext">Create a local paper account or sign in. No exchange account or real funds are used.</p>
          <div className="segmented"><button className={authMode === "signup" ? "selected" : ""} onClick={() => setAuthMode("signup")}>Create account</button><button className={authMode === "signin" ? "selected" : ""} onClick={() => setAuthMode("signin")}>Sign in</button></div>
          <form onSubmit={authenticate} className="stack-form">
            <label>USERNAME<input required minLength={1} maxLength={50} autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} placeholder="Choose a username" /></label>
            <label>PASSWORD<input required minLength={6} type="password" autoComplete={authMode === "signup" ? "new-password" : "current-password"} value={password} onChange={e => setPassword(e.target.value)} placeholder="At least 6 characters" /></label>
            <button className="primary-btn full" disabled={busy}>{busy ? "CONNECTING…" : authMode === "signup" ? "CREATE PAPER ACCOUNT" : "SIGN IN"}</button>
          </form>
          <div className="auth-note"><span className="dot good"/>API route · {API_BASE}</div>
        </section> : <>
          <section className="panel account-panel">
            <div className="panel-heading"><div><span className="eyebrow">ACCOUNT OVERVIEW</span><h2>{session.username}</h2></div><span className="private-status"><i className={privateStatus === "live" ? "dot good" : "dot"}/>{privateStatus.toUpperCase()}</span></div>
            <div className="account-total"><span>AVAILABLE COLLATERAL</span><strong>${money(balance.available)}</strong></div>
            <div className="account-grid"><div><span>LOCKED MARGIN</span><strong>${money(balance.locked)}</strong></div><div><span>DEMO WALLET</span><strong>${money(balance.wallet)}</strong></div></div>
            <div className="fund-form"><label>DEMO COLLATERAL AMOUNT<input type="number" min="1" step="1" value={fundAmount} onChange={e => setFundAmount(e.target.value)} /></label><div className="fund-actions"><button className="secondary-btn" onClick={fundAccount} disabled={busy}>ADD FUNDS</button><button className="outline-btn" onClick={withdrawFunds} disabled={busy}>WITHDRAW</button></div></div>
          </section>

          <section className="panel ticket-panel">
            <div className="panel-heading"><div><span className="eyebrow">ORDER ENTRY</span><h2>Limit order</h2></div><span className="contract-pill">ISOLATED</span></div>
            <div className="side-toggle"><button className={side === "long" ? "long selected" : "long"} onClick={() => { setSide("long"); setManualPrice(false); }}>BUY / LONG</button><button className={side === "short" ? "short selected" : "short"} onClick={() => { setSide("short"); setManualPrice(false); }}>SELL / SHORT</button></div>
            <form onSubmit={submitOrder} className="stack-form order-form">
              <label>ORDER TYPE<div className="type-select"><span>Limit</span><span className="disabled-type">Market · unavailable</span></div></label>
              <label>LIMIT PRICE <small>USD</small><input type="number" min="0.01" step="0.01" required value={price} onChange={e => { setManualPrice(true); setPrice(e.target.value); }} /></label>
              <label>QUANTITY <small>{market === "BTC-PERP" ? "BTC" : "ETH"}</small><input type="number" min="0.0001" step="0.0001" required value={qty} onChange={e => setQty(e.target.value)} /></label>
              <label>LEVERAGE<input type="range" min="1" max="20" value={leverage} onChange={e => setLeverage(e.target.value)} /><span className="range-value">{leverage}×</span></label>
              <div className="order-estimate"><span>EST. INITIAL MARGIN</span><strong>${money(Number(qty || 0) * Number(price || 0) / Number(leverage))}</strong></div>
              <button className={side === "long" ? "primary-btn full" : "danger-btn full"} disabled={busy || !privateStatus || privateStatus !== "live"}>{busy ? "SENDING…" : side === "long" ? `PLACE ${market} LONG` : `PLACE ${market} SHORT`}</button>
            </form>
          </section>

          <section className="panel position-panel">
            <div className="panel-heading"><div><span className="eyebrow">OPEN POSITION</span><h2>{accountPosition ? accountPosition.side?.toUpperCase() || (accountPosition.qty > 0 ? "LONG" : "SHORT") : "No position"}</h2></div>{accountPosition && <span className={accountPosition.unrealized_pnl >= 0 ? "pnl-positive" : "pnl-negative"}>{accountPosition.unrealized_pnl >= 0 ? "+" : ""}${money(accountPosition.unrealized_pnl)}</span>}</div>
            {accountPosition ? <><div className="position-details"><div><span>SIZE</span><strong>{quantity(Math.abs(accountPosition.qty))} {market === "BTC-PERP" ? "BTC" : "ETH"}</strong></div><div><span>ENTRY</span><strong>${money(accountPosition.entry_price)}</strong></div><div><span>LIQUIDATION</span><strong className="red-text">${money(accountPosition.liquidation_price)}</strong></div><div><span>MARGIN</span><strong>${money(accountPosition.margin)}</strong></div></div><button className="outline-btn full close-btn" onClick={closePosition} disabled={busy}>CLOSE POSITION</button></> : <p className="empty-copy">Your matched orders will appear here.</p>}
          </section>
        </>}
      </aside>
    </section>

    <section className="panel activity-panel">
      <div className="panel-heading"><div><span className="eyebrow">ACCOUNT EXECUTIONS</span><h2>Recent orders &amp; fills</h2></div><span className="personal-only">PRIVATE · {session ? session.username : "SIGN IN TO VIEW"}</span></div>
      <div className="activity-grid">
        <div><div className="table-head"><span>MARKET</span><span>SIDE</span><span>PRICE</span><span>SIZE</span><span>TIME</span></div>{fills.length ? fills.slice(0, 6).map(fill => <div className="table-row" key={fill.fill_id}><strong>{fill.market}</strong><span className={fill.side === "short" ? "red-text" : "green-text"}>{fill.side?.toUpperCase() || "MATCH"}</span><span>${money(fill.price)}</span><span>{quantity(fill.qty)}</span><span>{new Date(fill.timestamp_ms * 1000).toLocaleTimeString()}</span></div>) : <div className="table-empty">No personal fills yet. Place a limit order and this feed will update when the engine matches it.</div>}</div>
        <div className="orders-column"><div className="table-head"><span>RECENT ORDER</span><span>SIDE</span><span>STATUS</span></div>{orders.length ? orders.slice(0, 5).map(order => <div className="order-line" key={order.order_id}><strong>{order.market} <small>#{order.order_id.slice(0, 8)}</small></strong><span className={order.side === "short" ? "red-text" : "green-text"}>{order.side?.toUpperCase() || "—"}</span><span className={order.status === "filled" ? "green-text" : "muted"}>{order.status.replaceAll("_", " ").toUpperCase()}</span></div>) : <div className="table-empty">Order confirmations arrive over your private socket.</div>}</div>
      </div>
    </section>

    <footer><span>PYTHON PERPS · LOCAL SIMULATOR</span><span>Paper trading only. Prices use public market data; orders execute in the local matching engine.</span><a href={`${API_BASE}/docs`} target="_blank" rel="noreferrer">API DOCS ↗</a></footer>
  </main>;
}
