require('dotenv').config();
const express = require('express');
const crypto  = require('crypto');
const axios   = require('axios');
const jwt     = require('jsonwebtoken');
const path    = require('path');

const app  = express();
app.disable('x-powered-by');
const PORT = process.env.PORT || 3030;
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const BASE_URL   = 'https://api.india.delta.exchange';
const JWT_SECRET = process.env.JWT_SECRET || 'change_me_in_env';

const ACCOUNTS = [
  {
    id: 1,
    name: '1-Hour Option Sell',
    key:    process.env.DELTA_KEY_1,
    secret: process.env.DELTA_SECRET_1,
  },
  {
    id: 2,
    name: 'RSI Trading',
    key:    process.env.DELTA_KEY_2,
    secret: process.env.DELTA_SECRET_2,
  },
  {
    id: 3,
    name: 'RSI Option',
    key:    process.env.DELTA_KEY_3,
    secret: process.env.DELTA_SECRET_3,
  },
];

// ── Delta API helpers ────────────────────────────────────────────────────────

function buildHeaders(key, secret, method, urlPath, params = {}, body = '') {
  const ts  = String(Math.floor(Date.now() / 1000));
  const qs  = Object.keys(params).length
    ? Object.entries(params).map(([k, v]) => `${k}=${v}`).join('&')
    : '';
  const qsPart = qs ? `?${qs}` : '';
  const msg = method.toUpperCase() + ts + urlPath + qsPart + body;
  const sig = crypto.createHmac('sha256', secret).update(msg).digest('hex');
  return { 'api-key': key, signature: sig, timestamp: ts, 'Content-Type': 'application/json' };
}

async function deltaGet(key, secret, urlPath, params = {}) {
  const headers = buildHeaders(key, secret, 'GET', urlPath, params);
  const qs = Object.keys(params).length
    ? '?' + Object.entries(params).map(([k, v]) => `${k}=${v}`).join('&')
    : '';
  const res = await axios.get(`${BASE_URL}${urlPath}${qs}`, { headers, timeout: 15000 });
  return res.data;
}

// ── Auth middleware ──────────────────────────────────────────────────────────

function authMiddleware(req, res, next) {
  const header = req.headers.authorization;
  if (!header || !header.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  try {
    jwt.verify(header.split(' ')[1], JWT_SECRET);
    next();
  } catch {
    res.status(401).json({ error: 'Invalid token' });
  }
}

// ── Login ────────────────────────────────────────────────────────────────────

app.post('/api/login', (req, res) => {
  const { username, password } = req.body || {};
  if (username === 'jarvis' && password === 'Delta@888') {
    const token = jwt.sign({ user: 'jarvis' }, JWT_SECRET, { expiresIn: '24h' });
    return res.json({ token });
  }
  res.status(401).json({ error: 'Invalid credentials' });
});

// ── Account summary ──────────────────────────────────────────────────────────

app.get('/api/accounts', authMiddleware, async (req, res) => {
  try {
    const results = await Promise.all(ACCOUNTS.map(async (acc) => {
      try {
        // Wallet
        const walletData = await deltaGet(acc.key, acc.secret, '/v2/wallet/balances');
        const meta = walletData.meta || {};
        let balance = parseFloat(meta.net_equity || 0);
        let available = 0;
        for (const a of (walletData.result || [])) {
          if (a.asset_symbol === 'USD' || a.asset_symbol === 'USDT') {
            if (!balance) balance = parseFloat(a.balance || 0);
            available = parseFloat(a.available_balance || 0);
          }
        }

        // Positions
        let positions = [];
        try {
          const posData = await deltaGet(acc.key, acc.secret, '/v2/positions/margined');
          positions = (posData.result || []).filter(p => parseFloat(p.size || 0) !== 0);
        } catch {
          const posData2 = await deltaGet(acc.key, acc.secret, '/v2/positions');
          positions = (posData2.result || []).filter(p => parseFloat(p.size || 0) !== 0);
        }

        const unrealizedPnl = positions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0);

        // Open orders (all states=open)
        const ordersData = await deltaGet(acc.key, acc.secret, '/v2/orders', { state: 'open', page_size: 100 });
        const allOrders  = ordersData.result || [];
        const openOrders = allOrders.filter(o => !o.stop_order_type);
        const stopOrders = allOrders.filter(o => !!o.stop_order_type);

        return {
          id: acc.id,
          name: acc.name,
          balance: parseFloat(balance.toFixed(4)),
          available: parseFloat(available.toFixed(4)),
          unrealizedPnl: parseFloat(unrealizedPnl.toFixed(4)),
          positions: positions.map(p => ({
            symbol:         p.product_symbol || p.symbol || '',
            side:           p.size > 0 ? 'long' : 'short',
            size:           Math.abs(parseFloat(p.size || 0)),
            entryPrice:     parseFloat(p.entry_price || 0),
            markPrice:      parseFloat(p.mark_price || 0),
            unrealizedPnl:  parseFloat(p.unrealized_pnl || 0),
            liquidationPx:  parseFloat(p.liquidation_price || 0),
            leverage:       parseFloat(p.leverage || p.effective_leverage || 0),
          })),
          openOrders: openOrders.map(o => ({
            id:         o.id,
            symbol:     o.product_symbol || '',
            side:       o.side,
            size:       parseFloat(o.size || 0),
            price:      parseFloat(o.limit_price || o.price || 0),
            type:       o.order_type,
            state:      o.state,
            created_at: o.created_at,
          })),
          stopOrders: stopOrders.map(o => ({
            id:         o.id,
            symbol:     o.product_symbol || '',
            side:       o.side,
            size:       parseFloat(o.size || 0),
            stopPrice:  parseFloat(o.stop_price || 0),
            type:       o.stop_order_type,
            state:      o.state,
            created_at: o.created_at,
          })),
          error: null,
        };
      } catch (err) {
        return { id: acc.id, name: acc.name, error: err.message };
      }
    }));

    res.json({ accounts: results, fetchedAt: new Date().toISOString() });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── PnL by period ────────────────────────────────────────────────────────────

function getPeriodRange(period) {
  const now   = Math.floor(Date.now() / 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const t0    = Math.floor(today.getTime() / 1000);

  switch (period) {
    case 'today':      return { start: t0,              end: now };
    case 'yesterday':  return { start: t0 - 86400,      end: t0 };
    case '7d':         return { start: now - 7 * 86400, end: now };
    case 'this_week': {
      const d = new Date(today); d.setDate(d.getDate() - d.getDay());
      return { start: Math.floor(d.getTime() / 1000), end: now };
    }
    case 'last_week': {
      const d = new Date(today); d.setDate(d.getDate() - d.getDay() - 7);
      const dEnd = new Date(d); dEnd.setDate(dEnd.getDate() + 7);
      return { start: Math.floor(d.getTime() / 1000), end: Math.floor(dEnd.getTime() / 1000) };
    }
    case 'this_month': {
      const d = new Date(today.getFullYear(), today.getMonth(), 1);
      return { start: Math.floor(d.getTime() / 1000), end: now };
    }
    case 'last_month': {
      const d    = new Date(today.getFullYear(), today.getMonth() - 1, 1);
      const dEnd = new Date(today.getFullYear(), today.getMonth(), 1);
      return { start: Math.floor(d.getTime() / 1000), end: Math.floor(dEnd.getTime() / 1000) };
    }
    case '3m': return { start: now - 90 * 86400, end: now };
    case 'all': return { start: 0, end: now };
    default:    return { start: t0, end: now };
  }
}

app.get('/api/pnl', authMiddleware, async (req, res) => {
  const period = req.query.period || 'today';
  const { start, end } = getPeriodRange(period);

  try {
    const results = await Promise.all(ACCOUNTS.map(async (acc) => {
      try {
        const params = { page_size: 500 };
        if (start > 0) params.start_time = start;
        if (end)       params.end_time   = end;

        const fillsData = await deltaGet(acc.key, acc.secret, '/v2/fills', params);
        const fills = fillsData.result || [];

        const realizedPnl = fills.reduce((s, f) => s + parseFloat(f.realized_pnl || 0), 0);
        const commission  = fills.reduce((s, f) => s + parseFloat(f.commission || 0), 0);

        return {
          id:          acc.id,
          name:        acc.name,
          realizedPnl: parseFloat(realizedPnl.toFixed(4)),
          commission:  parseFloat(commission.toFixed(4)),
          netPnl:      parseFloat((realizedPnl - commission).toFixed(4)),
          fillCount:   fills.length,
          error:       null,
        };
      } catch (err) {
        return { id: acc.id, name: acc.name, realizedPnl: 0, netPnl: 0, fillCount: 0, error: err.message };
      }
    }));

    const totalPnl = results.reduce((s, r) => s + r.netPnl, 0);
    res.json({ period, accounts: results, totalPnl: parseFloat(totalPnl.toFixed(4)) });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Health ───────────────────────────────────────────────────────────────────

app.get('/api/health', (req, res) => res.json({ status: 'ok', time: new Date().toISOString() }));

app.listen(PORT, '127.0.0.1', () => console.log(`Jarvis Dashboard → http://127.0.0.1:${PORT}`));
