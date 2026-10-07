/* 手机当键盘 - 前端逻辑 */
(() => {
  const TOKEN = new URLSearchParams(location.search).get('t') || '';
  const dot = document.getElementById('dot');
  const statusEl = document.getElementById('status');
  const ta = document.getElementById('ta');
  const realtimeEl = document.getElementById('realtime');
  const sendBtn = document.getElementById('send');
  const clearBtn = document.getElementById('clear');

  // ---------- 连接 ----------
  let ws = null;
  let retry = 0;
  let ready = false;

  function setStatus(text, ok) {
    statusEl.textContent = text;
    dot.classList.toggle('on', !!ok);
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);

    ws.onopen = () => { ready = true; retry = 0; setStatus('已连接', true); };
    ws.onclose = () => {
      ready = false;
      setStatus('已断开，重连中…', false);
      retry = Math.min(retry + 1, 10);
      setTimeout(connect, 500 * retry);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  function send(obj) {
    if (ready && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  // ---------- 实时 diff 同步 ----------
  // sent = 我们认为电脑端当前已有的内容
  let sent = '';
  let composing = false;

  function sync() {
    const cur = ta.value;

    let p = 0;
    while (p < cur.length && p < sent.length && cur[p] === sent[p]) p++;

    let s = 0;
    while (s < cur.length - p && s < sent.length - p &&
           cur[cur.length - 1 - s] === sent[sent.length - 1 - s]) s++;

    const del = sent.length - p - s;
    const ins = cur.substring(p, cur.length - s);

    if (del === 0 && ins === '') return;

    if (del > 0) send({ type: 'key', key: 'backspace', count: del });
    if (ins) send({ type: 'text', data: ins });
    sent = cur;
  }

  ta.addEventListener('compositionstart', () => { composing = true; });
  ta.addEventListener('compositionend', () => { composing = false; if (realtimeEl.checked) sync(); });
  ta.addEventListener('input', () => {
    if (composing) return;              // 拼音候选阶段不同步
    if (realtimeEl.checked) sync();
  });

  // ---------- 特殊键 ----------
  const KEYS = [
    { label: '⌫', key: 'backspace' },
    { label: '⏎', key: 'enter' },
    { label: '⇥', key: 'tab' },
    { label: 'Esc', key: 'esc' },
    { label: '←', key: 'left' },
    { label: '↑', key: 'up' },
    { label: '↓', key: 'down' },
    { label: '→', key: 'right' },
    { label: 'Home', key: 'home' },
    { label: 'End', key: 'end' },
    { label: 'Ctrl+A', mods: ['ctrl'], key: 'a' },
    { label: 'Ctrl+C', mods: ['ctrl'], key: 'c' },
    { label: 'Ctrl+V', mods: ['ctrl'], key: 'v' },
    { label: 'Ctrl+S', mods: ['ctrl'], key: 's' },
    { label: 'Ctrl+Z', mods: ['ctrl'], key: 'z' },
    { label: 'Ctrl+F', mods: ['ctrl'], key: 'f' },
  ];

  const keysBox = document.getElementById('keys');
  KEYS.forEach((k) => {
    const b = document.createElement('button');
    b.textContent = k.label;
    b.tabIndex = -1;
    b.addEventListener('pointerdown', (e) => e.preventDefault()); // 保住焦点/键盘
    b.addEventListener('click', () => {
      if (k.mods) send({ type: 'combo', mods: k.mods, key: k.key });
      else send({ type: 'key', key: k.key });
    });
    keysBox.appendChild(b);
  });

  // ---------- 手动模式 / 清空 ----------
  function updateMode() {
    sendBtn.style.display = realtimeEl.checked ? 'none' : 'block';
  }
  realtimeEl.addEventListener('change', () => {
    updateMode();
    if (realtimeEl.checked) sync();
  });

  sendBtn.addEventListener('click', () => {
    const text = ta.value;
    if (!text) return;
    send({ type: 'text', data: text });
    sent = text;
  });

  clearBtn.addEventListener('pointerdown', (e) => e.preventDefault()); // 不失焦、不闪键盘
  clearBtn.addEventListener('click', () => {
    ta.value = '';
    sent = '';      // 只清本地显示，不影响电脑上已经打好的内容
    ta.focus();
  });

  updateMode();
  connect();
})();
