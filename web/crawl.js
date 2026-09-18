/* 手动爬取：悬浮按钮 + 进度抽屉 + 轮询。
 *
 * 用轮询而非 SSE/WebSocket：局域网延迟极低，1s 轮询完全够用，
 * 而且省掉了手机浏览器切到后台再回来时的长连接重连逻辑。
 */
(function (global) {
  'use strict';

  const POLL_MS = 1000;
  let pollTimer = null;
  let currentRunId = null;

  const el = (id) => document.getElementById(id);

  function toast(msg, ms = 3200) {
    const node = el('toast');
    node.textContent = msg;
    node.hidden = false;
    clearTimeout(node._t);
    node._t = setTimeout(() => { node.hidden = true; }, ms);
  }

  function openDrawer() { el('crawl-drawer').hidden = false; }
  function closeDrawer() {
    el('crawl-drawer').hidden = true;
    stopPolling();
  }

  const STATUS_ICON = {
    pending: '○', running: '◐', success: '✓', failed: '✕', skipped: '–',
  };
  const STATUS_CLASS = {
    success: 'ok', failed: 'failing', running: 'degraded', pending: 'unknown',
  };

  function renderRun(data) {
    const run = data.run || {};
    const items = data.items || [];

    const done = run.finished_sources || 0;
    const total = run.total_sources || items.length;
    let summary = `批次 #${run.id} · ${run.trigger === 'schedule' ? '定时' : '手动'} · `;
    if (run.status === 'running') {
      summary += `进行中 ${done}/${total}`;
    } else {
      const label = { success: '全部成功', partial: '部分成功', failed: '失败' }[run.status] || run.status;
      summary += `${label} · 新增 ${run.total_new} 条，更新 ${run.total_updated} 条`;
    }
    el('crawl-summary').textContent = summary;

    el('crawl-items').innerHTML = items.map((it) => {
      const secs = it.duration_ms ? (it.duration_ms / 1000).toFixed(1) + 's' : '';
      let meta = secs;
      if (it.status === 'success') meta = `${it.items_found} 条 ${secs}`;
      const err = it.error ? `<div class="err" style="color:var(--danger);font-size:12px">${escapeHtml(it.error)}</div>` : '';
      return `<div class="run-item">
        <span class="dot ${STATUS_CLASS[it.status] || 'unknown'}"></span>
        <span class="name">${escapeHtml(it.source_name || it.source_key)}${err}</span>
        <span class="meta">${STATUS_ICON[it.status] || ''} ${meta}</span>
      </div>`;
    }).join('');

    return run.status !== 'running';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function pollOnce() {
    if (!currentRunId) return;
    try {
      const res = await fetch(`/api/crawl/runs/${currentRunId}`);
      if (!res.ok) { stopPolling(); return; }
      const data = await res.json();
      const finished = renderRun(data);
      if (finished) {
        stopPolling();
        setFabBusy(false);
        const run = data.run;
        if (run.status === 'success') {
          toast(`爬取完成，新增 ${run.total_new} 条`);
        } else {
          toast('爬取结束，部分数据源失败，详见进度面板', 5000);
        }
        if (global.App && global.App.refresh) global.App.refresh();
      }
    } catch (e) {
      stopPolling();
      setFabBusy(false);
    }
  }

  function startPolling(runId) {
    currentRunId = runId;
    stopPolling();
    pollTimer = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function setFabBusy(busy) {
    const fab = el('crawl-fab');
    fab.disabled = busy;
    fab.classList.toggle('spinning', busy);
    fab.querySelector('.fab-text').textContent = busy ? '爬取中…' : '立即爬取';
  }

  async function trigger() {
    setFabBusy(true);
    openDrawer();
    el('crawl-summary').textContent = '正在启动…';
    el('crawl-items').innerHTML = '';

    try {
      const res = await fetch('/api/crawl/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'incremental' }),
      });
      const data = await res.json();

      if (res.status === 409) {
        // 已有任务在跑：接续它的进度，而不是报错 —— 手机上点第二次很常见
        startPolling(data.run_id);
        return;
      }
      if (!res.ok) {
        toast(data.detail || '启动失败');
        setFabBusy(false);
        return;
      }
      if (data.status === 'already_running') {
        toast('已有任务在执行，正在接续进度');
      }
      startPolling(data.run_id);
    } catch (e) {
      toast('无法连接到服务');
      setFabBusy(false);
    }
  }

  /* 页面加载时若已有任务在跑（例如另一台设备触发的），自动接续轮询 */
  async function resumeIfRunning() {
    try {
      const res = await fetch('/api/crawl/status');
      if (!res.ok) return;
      const data = await res.json();
      if (data.running && data.run_id) {
        setFabBusy(true);
        openDrawer();
        startPolling(data.run_id);
      }
    } catch (e) { /* 忽略：状态查询失败不影响正常浏览 */ }
  }

  document.addEventListener('DOMContentLoaded', () => {
    el('crawl-fab').addEventListener('click', trigger);
    el('drawer-close').addEventListener('click', closeDrawer);
    resumeIfRunning();
  });

  global.Crawl = { toast, trigger, escapeHtml, resumeIfRunning };
})(window);
