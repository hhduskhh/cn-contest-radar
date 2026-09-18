/* 历年时间线：每年一行，横轴 12 个月，每个赛事一条泳道。
 *
 * 用两条色条表达报名期（浅色）与比赛期（深色）。跨年的区间在行尾画箭头
 * 连到下一行，把「10 月报名、次年 4 月比赛」这个备赛节奏直接暴露出来。
 *
 * 纯 CSS 定位，不引图表库。
 */
(function (global) {
  'use strict';

  const MONTHS = ['1月', '2月', '3月', '4月', '5月', '6月',
                  '7月', '8月', '9月', '10月', '11月', '12月'];

  const el = (id) => document.getElementById(id);
  const esc = (s) => (global.Crawl ? global.Crawl.escapeHtml(s) : String(s || ''));

  function parseDate(s) {
    if (!s) return null;
    const parts = String(s).slice(0, 10).split('-').map(Number);
    if (parts.length !== 3 || parts.some(isNaN)) return null;
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  /** 把区间裁剪到某一年内，返回百分比坐标。完全落在年外返回 null。 */
  function barBox(start, end, year) {
    if (!start) return null;
    let s = start;
    let e = end && end > start ? end : start;
    // 比赛常常是当天开始当天结束，给个最小可见宽度
    const yearStart = new Date(year, 0, 1);
    const yearEnd = new Date(year + 1, 0, 1);

    if (e < yearStart && s < yearStart) return null;
    if (s >= yearEnd) return null;

    const clippedStart = s < yearStart ? yearStart : s;
    const clippedEnd = e > yearEnd ? yearEnd : e;

    const total = yearEnd - yearStart;
    const left = ((clippedStart - yearStart) / total) * 100;
    const width = Math.max(((clippedEnd - clippedStart) / total) * 100, 1.4);

    return {
      left: Math.max(0, left),
      width: Math.min(width, 100 - Math.max(0, left)),
      spillsLeft: s < yearStart,
      spillsRight: e >= yearEnd,
    };
  }

  function barHtml(cls, box, title, href) {
    if (!box) return '';
    const style = `left:${box.left}%;width:${box.width}%`;
    const mark = box.spillsRight ? ' ▸' : (box.spillsLeft ? '◂ ' : '');
    const tip = esc(title + mark);
    // 有来源链接时用 <a>，点一下直接跳到官网/原始通知
    if (href) {
      return `<a class="tl-bar ${cls} linked" style="${style}" title="${tip}"
                 href="${esc(href)}" target="_blank" rel="noopener"></a>`;
    }
    return `<div class="tl-bar ${cls}" style="${style}" title="${tip}"></div>`;
  }

  function tipFor(row) {
    const bits = [];
    bits.push(row.origin === 'manual' ? '数据来源：人工整理' : '数据来源：官网爬取');
    if (row.access_label) bits.push('参赛方式：' + row.access_label);
    const conf = { high: '高（接口结构化字段）', medium: '中（结构化页面）',
                   low: '低（正文正则抽取）' }[row.time_confidence] || row.time_confidence;
    bits.push('时间置信度：' + conf);
    if (row.registration_raw) bits.push('报名原文：' + row.registration_raw);
    if (row.contest_raw) bits.push('比赛原文：' + row.contest_raw);
    if (row.source_note) bits.push('备注：' + row.source_note);
    if (row.source_url) bits.push('点击色条可打开来源页面');
    return bits.join('\n');
  }

  function render(container, rows, events, opts) {
    // 按 赛事 → 年份 组织。同一赛事同一年可能有多个赛道（分站赛），各占一条泳道。
    const lanes = new Map();
    for (const row of rows) {
      const key = `${row.event_slug}::${row.lane_label}`;
      if (!lanes.has(key)) {
        lanes.set(key, { label: row.lane_label, slug: row.event_slug, byYear: new Map() });
      }
      const lane = lanes.get(key);
      if (!lane.byYear.has(row.year)) lane.byYear.set(row.year, []);
      lane.byYear.get(row.year).push(row);
    }

    // 泳道顺序：按赛事在 events 列表里的顺序，保持稳定
    const order = new Map(events.map((e, i) => [e.slug, i]));
    const sortedLanes = [...lanes.values()].sort((a, b) => {
      const d = (order.get(a.slug) ?? 99) - (order.get(b.slug) ?? 99);
      return d !== 0 ? d : a.label.localeCompare(b.label, 'zh');
    });

    const years = [];
    for (let y = opts.toYear; y >= opts.fromYear; y--) years.push(y);

    let html = '';
    for (const year of years) {
      const active = sortedLanes.filter((lane) => lane.byYear.has(year));
      if (!active.length) continue;

      html += `<div class="tl-year-label">${year} 年</div>`;
      for (const lane of active) {
        const rowsOfLane = lane.byYear.get(year);
        const bars = rowsOfLane.map((row) => {
          const tip = tipFor(row);
          const origin = row.origin === 'manual' ? ' manual' : '';
          const regBox = barBox(parseDate(row.registration_start),
                                parseDate(row.registration_end), year);
          const conBox = barBox(parseDate(row.contest_start),
                                parseDate(row.contest_end), year);
          const hasNothing = !regBox && !conBox;
          const href = row.source_url || row.official_url || null;
          if (hasNothing) {
            // 没有时间不等于没有信息：该届已启动但赛程未公布，本身就是要传达的信号。
            // 依然可点击，跳去来源看原文。
            const inner = '<div class="tl-bar none" style="left:0;width:100%" title="'
              + esc(tip + '\n该届已启动，时间待公布') + '"></div>';
            return href
              ? `<a class="tl-bar-link" href="${esc(href)}" target="_blank" rel="noopener">${inner}</a>`
              : inner;
          }
          return barHtml('reg' + origin, regBox, tip, href)
            + barHtml('con' + origin, conBox, tip, href);
        }).join('');

        html += `<div class="tl-lane">
          <div class="tl-lane-name" title="${esc(lane.label)}">${esc(lane.label)}</div>
          <div class="tl-track">${bars}</div>
        </div>`;
      }
    }

    if (!html) {
      html = '<div class="empty">所选条件下没有数据。试试放宽年份范围或关闭「只看广东」。</div>';
    }

    container.innerHTML = html;
  }

  global.Timeline = { render, MONTHS };
})(window);
