/* 单页应用：hash 路由 + 四个视图。无构建步骤。 */
(function (global) {
  'use strict';

  const el = (id) => document.getElementById(id);
  const esc = (s) => (global.Crawl ? global.Crawl.escapeHtml(s) : String(s == null ? '' : s));

  const CATEGORY_LABEL = {
    algorithm: '程序设计', ai: '人工智能', bigdata: '大数据',
    robotics: '机器人', design: '设计',
  };
  const STATUS_LABEL = {
    registering: '报名中', running: '进行中', upcoming: '即将开始', finished: '已结束',
  };
  const CONF_LABEL = { high: '置信度高', medium: '置信度中', low: '置信度低' };

  const state = {
    // 默认**不过滤**。判断标准是「能不能在广东参加」，而不是「比赛地在不在广东」——
    // CCPC 分站赛决赛虽在外省，初赛（网络赛）是线上的，广东学生照样能参加。
    gdOnly: localStorage.getItem('gdOnly') === '1',
    events: [],
    lastView: null,
  };

  /* ---------------- 工具 ---------------- */

  function api(path, params) {
    const url = new URL(path, location.origin);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
      }
    }
    if (state.gdOnly) url.searchParams.set('region_filter', 'guangdong');
    return fetch(url).then((r) => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  const fmtRange = (a, b) => {
    if (!a && !b) return '—';
    if (a && b && a !== b) return `${a} ~ ${b}`;
    return a || b;
  };

  // 跨度过大的区间标成「长期开放」而不是照搬 2099 这种哨兵值
  const rangeText = (row, startKey, endKey) => {
    if (row.is_long_running) {
      return row[startKey] ? `${row[startKey]} 起（长期开放）` : '长期开放';
    }
    return fmtRange(row[startKey], row[endKey]);
  };

  function badges(row, opts) {
    const out = [];
    if (row.status && STATUS_LABEL[row.status]) {
      out.push(`<span class="badge ${row.status}">${STATUS_LABEL[row.status]}</span>`);
    }
    // 「线上参加」是绝大多数赛事的情况，默认不显示，否则整页都是同一个标签；
    // 需要时（详情页表格）用 opts.showOnline 打开。
    if (row.access === 'guangdong') {
      out.push('<span class="badge gd">广东线下</span>');
    } else if (row.access === 'travel') {
      out.push('<span class="badge travel" title="决赛在外省，但初赛（网络赛）可线上参加">需出省</span>');
    } else if (row.access === 'online' && opts && opts.showOnline) {
      out.push('<span class="badge">线上参加</span>');
    }
    if (row.origin === 'manual') out.push('<span class="badge manual">人工整理</span>');
    return `<div class="badges">${out.join('')}</div>`;
  }

  function sourceNote(s) {
    if (s.source_note) return ` · ${esc(s.source_note)}`;
    if (s.time_confidence) return ` · ${CONF_LABEL[s.time_confidence] || ''}`;
    return '';
  }

  /* ---------------- 视图 1：总览 ---------------- */

  async function viewOverview(root) {
    root.innerHTML = '<div class="loading">加载中…</div>';

    const data = await api('/api/dashboard');

    // 预计时间是这一页的主角，给它单独的视觉层级
    const forecastLine = (f) => {
      if (!f) return '<span class="fc-none">暂无日期数据</span>';
      if (f.fallback) {
        return `<span class="fc-warn">无报名数据</span> 比赛通常在 <b>${f.contest.label}</b>`;
      }
      const r = f.registration;
      return `预计 <b>${r.label}</b> 开始报名`;
    };

    const historyTable = (rows) => `
      <table class="hist">
        <thead><tr><th>年份</th><th>报名时间</th><th>比赛时间</th><th></th></tr></thead>
        <tbody>${rows.map((h) => `
          <tr>
            <td class="hist-year">${h.year}</td>
            <td class="hist-date">${h.registration || '—'}</td>
            <td class="hist-date">${h.contest || '—'}</td>
            <td class="hist-note">
              ${h.subs ? `<span class="subs">${h.subs} 场</span>` : ''}
              ${h.source_url ? `<a class="ext-link" href="${esc(h.source_url)}"
                 target="_blank" rel="noopener">原文↗</a>` : ''}
            </td>
          </tr>`).join('')}</tbody>
      </table>`;

    const eventBlock = (e) => `
      <div class="ev">
        <div class="ev-head">
          <a class="ev-name" href="#/event/${encodeURIComponent(e.slug)}">${esc(e.name)}</a>
          ${e.official_url ? `<a class="ext-link" href="${esc(e.official_url)}"
             target="_blank" rel="noopener">官网↗</a>` : ''}
        </div>
        <div class="ev-forecast">${forecastLine(e.forecast)}${
          e.forecast ? ` <em>基于 ${e.forecast.based_on} 年数据</em>` : ''
        }</div>
        ${e.history.length ? historyTable(e.history)
          : '<div class="hint">该赛事官网只公布了赛站名单，没有日期</div>'}
      </div>`;

    // 折叠用原生 <details>，不写 JS
    const groupsHtml = data.groups.map((g) => `
      <details class="cat" open>
        <summary>
          <span class="cat-name">${esc(g.label)}</span>
          <span class="cat-count">${g.events.length} 个赛事</span>
        </summary>
        ${g.events.map(eventBlock).join('')}
      </details>`).join('');

    const platformHtml = data.platform.length ? `
      <details class="cat">
        <summary>
          <span class="cat-name">平台赛事 · 当前可报名</span>
          <span class="cat-count">${data.platform.length} 场</span>
        </summary>
        <ul class="open-list">${data.platform.map((p) => `
          <li>
            <span class="days ${p.days_left !== null && p.days_left <= 7 ? 'soon' : ''}">${
              p.days_left !== null ? p.days_left + ' 天' : '—'}</span>
            <span class="o-title">${esc(p.title)}</span>
            <span class="o-meta">${esc(p.event_name)} · 截止 ${p.registration_end || '—'}</span>
            ${p.url ? `<a class="ext-link" href="${esc(p.url)}" target="_blank" rel="noopener">官网↗</a>` : ''}
          </li>`).join('')}</ul>
      </details>` : '';

    root.innerHTML = `
      <h1 class="page-title">报名时间节点</h1>
      <div class="page-sub">历年报名与比赛日期，用来推算今年的报名时间。</div>
      ${groupsHtml}
      ${platformHtml}
    `;
  }

  /* ---------------- 视图 2：历年时间线 ---------------- */

  async function viewTimeline(root) {
    root.innerHTML = '<div class="loading">加载中…</div>';

    const events = state.events.length ? state.events : (await api('/api/events')).events;
    state.events = events;

    const currentYear = new Date().getFullYear();
    const fromYear = Number(localStorage.getItem('tlFrom') || (currentYear - 5));
    const toYear = Number(localStorage.getItem('tlTo') || currentYear);

    if (!root._timelineBuilt) {
      root._timelineBuilt = true;
      const chips = events.map((e) => `
        <label class="gd-toggle" style="border-color:var(--line);color:var(--ink-2)">
          <input type="checkbox" class="tl-ev" value="${esc(e.slug)}"> <span>${esc(e.name.slice(0, 10))}</span>
        </label>`).join('');

      root.innerHTML = `
        <h1 class="page-title">历年赛事时间线</h1>
        <div class="page-sub">
          每年一行，横轴 12 个月。浅色条是报名期，深色条是比赛期；跨年的区间会在行尾断开并在下一年继续。
        </div>
        <div class="tl-controls">
          <label>从 <input type="number" id="tl-from" value="${fromYear}" min="2015" max="2030" style="width:76px"></label>
          <label>到 <input type="number" id="tl-to" value="${toYear}" min="2015" max="2030" style="width:76px"></label>
          <button class="btn" id="tl-apply">应用</button>
          <button class="btn" id="tl-all">全选赛事</button>
          <button class="btn" id="tl-none">清空</button>
        </div>
        <div class="tl-controls" style="gap:6px">${chips}</div>
        <div class="tl-legend">
          <span><i class="reg"></i>报名期</span>
          <span><i class="con"></i>比赛期</span>
          <span><i class="manual"></i>人工整理</span>
          <span><i class="none"></i>时间待公布</span>
          <span>鼠标悬停或手机点按色条可看原文与来源</span>
        </div>
        <div class="tl-scroll" id="tl-scroll"><div class="tl-grid" id="tl-grid"></div></div>
      `;

      const selected = new Set(JSON.parse(localStorage.getItem('tlSlugs') || '["lanqiao","ccpc","tianchi"]'));
      root.querySelectorAll('.tl-ev').forEach((cb) => { cb.checked = selected.has(cb.value); });

      // 必须走 apply()：它会把年份输入存进 localStorage 再重建，
      // 直接 viewTimeline() 会读回旧值，用户改的年份会被静默忽略
      root.querySelector('#tl-apply').addEventListener('click', apply);
      root.querySelector('#tl-all').addEventListener('click', () => {
        root.querySelectorAll('.tl-ev').forEach((cb) => { cb.checked = true; }); apply();
      });
      root.querySelector('#tl-none').addEventListener('click', () => {
        root.querySelectorAll('.tl-ev').forEach((cb) => { cb.checked = false; }); apply();
      });
      root.querySelectorAll('.tl-ev').forEach((cb) => cb.addEventListener('change', apply));

      function apply() {
        const checked = [...root.querySelectorAll('.tl-ev:checked')].map((cb) => cb.value);
        localStorage.setItem('tlSlugs', JSON.stringify(checked));
        localStorage.setItem('tlFrom', root.querySelector('#tl-from').value);
        localStorage.setItem('tlTo', root.querySelector('#tl-to').value);
        root._timelineBuilt = false;
        viewTimeline(root);
      }
    }

    const slugs = [...root.querySelectorAll('.tl-ev:checked')].map((cb) => cb.value);
    const f = Number(root.querySelector('#tl-from').value);
    const t = Number(root.querySelector('#tl-to').value);

    const data = await api('/api/timeline', {
      slugs: slugs.join(','), from_year: f, to_year: t,
    });

    // 月份轴每次都重建（年份可能变了）
    const grid = root.querySelector('#tl-grid');
    const axis = `<div class="tl-axis"><div>赛事 \\ 月份</div>${global.Timeline.MONTHS.map((m) => `<div>${m}</div>`).join('')}</div>`;

    const holder = document.createElement('div');
    global.Timeline.render(holder, data.editions || [], events, { fromYear: f, toYear: t });
    grid.innerHTML = axis + holder.innerHTML;

    // 手机默认滚到当前月份
    const scroller = root.querySelector('#tl-scroll');
    if (scroller && window.innerWidth < 640) {
      const month = new Date().getMonth();
      scroller.scrollLeft = Math.max(0, (132 + (scroller.scrollWidth - 132) * (month / 12)) - window.innerWidth / 2);
    }
  }

  /* ---------------- 视图 3：数据源 ---------------- */

  async function viewSources(root) {
    root.innerHTML = '<div class="loading">加载中…</div>';
    const [sources, runs] = await Promise.all([api('/api/crawl/sources'), api('/api/crawl/runs', { limit: 8 })]);

    const HEALTH_LABEL = {
      ok: '正常', degraded: '降级', failing: '失败', needs_reauth: '需要重新登录', unknown: '未运行',
    };

    const rows = (sources.sources || []).map((s) => {
      const note = s.note ? `<div style="font-size:12px;color:var(--warn);margin-top:3px">${esc(s.note)}</div>` : '';
      const err = s.last_error ? `<div class="err">${esc(s.last_error)}</div>` : '';
      return `<tr>
        <td><span class="dot ${s.health}"></span>${esc(s.name)}</td>
        <td>${HEALTH_LABEL[s.health] || s.health}${note}</td>
        <td>${esc((s.last_success_at || '').replace('T', ' ').slice(0, 16) || '从未')}</td>
        <td>${s.consecutive_failures || 0}</td>
        <td>${err}</td>
      </tr>`;
    }).join('');

    const runRows = (runs.runs || []).map((r) => {
      const label = { success: '成功', partial: '部分成功', failed: '失败', running: '进行中' }[r.status] || r.status;
      return `<tr>
        <td>#${r.id}</td>
        <td>${r.trigger === 'schedule' ? '定时' : '手动'}</td>
        <td>${r.mode === 'backfill' ? '全量' : '增量'}</td>
        <td>${label}</td>
        <td>${r.total_new} / ${r.total_updated}</td>
        <td>${esc((r.started_at || '').replace('T', ' ').slice(0, 16))}</td>
      </tr>`;
    }).join('');

    root.innerHTML = `
      <h1 class="page-title">数据源</h1>
      <div class="page-sub">每个源独立抓取，单个源失败不影响其它源。点右下角「立即爬取」可随时手动触发。</div>

      <div class="section-title">源健康状态</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>数据源</th><th>状态</th><th>上次成功</th><th>连续失败</th><th>最近错误</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>

      <div class="section-title">最近的爬取批次</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>批次</th><th>触发</th><th>模式</th><th>结果</th><th>新增/更新</th><th>开始时间</th></tr></thead>
          <tbody>${runRows || '<tr><td colspan="6">还没有爬取记录</td></tr>'}</tbody>
        </table>
      </div>
    `;
  }

  /* ---------------- 视图 4：赛事详情 ---------------- */

  async function viewEvent(root, slug) {
    root.innerHTML = '<div class="loading">加载中…</div>';
    const data = await api(`/api/events/${encodeURIComponent(slug)}`);
    const ev = data.event;
    const eds = data.editions || [];

    const rows = eds.map((r) => `
      <tr>
        <td>${r.year}</td>
        <td>${esc(r.title || r.sub_event || '—')}</td>
        <td>${rangeText(r, 'registration_start', 'registration_end')}${sourceNote(r)}</td>
        <td>${rangeText(r, 'contest_start', 'contest_end')}</td>
        <td>${badges(r)}</td>
        <td>${r.source_url ? `<a href="${esc(r.source_url)}" target="_blank" rel="noopener">来源</a>` : '—'}</td>
      </tr>`).join('');

    const notices = (data.notices || []).map((n) => `
      <li>
        <span class="notice-date">${esc((n.published_at || '').slice(0, 10))}</span>
        ${n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}
      </li>`).join('');

    root.innerHTML = `
      <a class="detail-back" href="#/">← 返回总览</a>
      <h1 class="page-title">${esc(ev.name)}</h1>
      <div class="page-sub">
        ${CATEGORY_LABEL[ev.category] || ev.category}
        ${ev.organizer ? ' · ' + esc(ev.organizer) : ''}
        ${ev.official_url ? ` · <a href="${esc(ev.official_url)}" target="_blank" rel="noopener">官网</a>` : ''}
      </div>
      ${ev.description ? `<div class="card" style="margin-bottom:16px">${esc(ev.description)}</div>` : ''}

      <div class="section-title">历届记录 <span class="count">${eds.length} 条</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>年份</th><th>赛道</th><th>报名时间</th><th>比赛时间</th><th>标注</th><th>来源</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="6">暂无记录</td></tr>'}</tbody>
        </table>
      </div>

      <div class="section-title">相关通知 <span class="count">${(data.notices || []).length} 条</span></div>
      <div class="card"><ul class="notice-list">${notices || '<li>暂无</li>'}</ul></div>
    `;
  }

  /* ---------------- 路由 ---------------- */

  async function route() {
    const root = el('app');
    const hash = location.hash.replace(/^#/, '') || '/';

    document.querySelectorAll('.nav a').forEach((a) => a.classList.remove('active'));

    try {
      if (hash.startsWith('/event/')) {
        await viewEvent(root, decodeURIComponent(hash.slice('/event/'.length)));
      } else if (hash === '/timeline') {
        document.querySelector('[data-nav="timeline"]').classList.add('active');
        await viewTimeline(root);
      } else if (hash === '/sources') {
        document.querySelector('[data-nav="sources"]').classList.add('active');
        await viewSources(root);
      } else {
        document.querySelector('[data-nav="overview"]').classList.add('active');
        await viewOverview(root);
      }
    } catch (err) {
      root.innerHTML = `<div class="empty">加载失败：${esc(err.message)}<br>
        <button class="btn" onclick="location.reload()" style="margin-top:12px">重试</button></div>`;
    }
    window.scrollTo(0, 0);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const box = el('gd-only');
    box.checked = state.gdOnly;
    box.addEventListener('change', () => {
      state.gdOnly = box.checked;
      localStorage.setItem('gdOnly', box.checked ? '1' : '0');
      route();
    });
    window.addEventListener('hashchange', route);
    route();
  });

  global.App = { refresh: route, api, esc };
})(window);
