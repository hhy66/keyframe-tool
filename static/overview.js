/* Whole-video overview above the results: editing rhythm in one sentence, a colour timeline (one
   segment per shot, as wide as the shot, in its main colour), shot lengths as bars, and the spread of
   shot sizes and camera movements. The spread doubles as a filter: click "特写" to see only close-ups.
   Uses `state`, `openLb` from app.js, `Review` (cursor) and `Analysis` (shots and picture analysis). */
const OVERVIEW_OPEN_KEY = 'keyframe-tool.overview.open';
const FAST_SHOT = 1; // 不到 1 秒算快切

// Editing rhythm of a list of shots (each {start, end, duration}). Pure, so it can be tested on its own.
function rhythmSummary(spans) {
  const shots = spans.filter(Boolean).sort((a, b) => a.start - b.start);
  if (!shots.length) return null;
  const lengths = shots.map(s => s.duration);
  const mean = values => values.reduce((a, b) => a + b, 0) / values.length;
  const average = mean(lengths);
  const sorted = [...lengths].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  const pace = average < 2 ? '快节奏（快切）' : average > 5 ? '慢节奏（长镜头为主）' : '中等节奏';
  const seconds = value => (value < 10 ? value.toFixed(1) : String(Math.round(value)));
  const notes = [];
  // Compare the first and last third of the video, by time.
  if (shots.length >= 6) {
    const begin = shots[0].start;
    const span = shots[shots.length - 1].end - begin;
    const part = k => shots.filter(s => (s.start - begin) / span >= k / 3 && (s.start - begin) / span < (k + 1) / 3);
    const first = part(0);
    const last = part(2);
    if (first.length && last.length) {
      const early = mean(first.map(s => s.duration));
      const late = mean(last.map(s => s.duration));
      if (late < early * 0.6) notes.push(`越往后越快：开头平均 ${seconds(early)} 秒一个镜头，结尾 ${seconds(late)} 秒`);
      else if (late > early * 1.6)
        notes.push(`越往后越慢：开头平均 ${seconds(early)} 秒一个镜头，结尾 ${seconds(late)} 秒`);
    }
  }
  // The longest run of quick cuts in a row.
  let run = [0, -1];
  for (let i = 0, from = 0; i <= shots.length; i++) {
    if (i < shots.length && shots[i].duration < FAST_SHOT) continue;
    if (i - from > run[1] - run[0] + 1) run = [from, i - 1];
    from = i + 1;
  }
  if (run[1] - run[0] + 1 >= 4) {
    notes.push(`第 ${run[0] + 1}–${run[1] + 1} 个镜头连续快切：${run[1] - run[0] + 1} 个镜头都不到 ${FAST_SHOT} 秒`);
  }
  const longest = lengths.indexOf(Math.max(...lengths));
  notes.push(`最长的是第 ${longest + 1} 个镜头，${seconds(lengths[longest])} 秒`);
  return {
    count: shots.length,
    average,
    median,
    pace,
    headline: `共 ${shots.length} 个镜头 · 平均 ${seconds(average)} 秒一个 · ${pace}`,
    notes,
  };
}

// How often each value appears, most common first: [[value, count], ...].
function tally(values) {
  const counts = new Map();
  for (const value of values) if (value) counts.set(value, (counts.get(value) || 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1]);
}

const Overview = {
  filter: null, // {kind: 'shot' | 'move', value}

  el(selector) {
    return document.querySelector(selector);
  },

  // Values a card can be filtered by.
  values(i) {
    const item = Analysis.items[i];
    const move = Analysis.shots[i]?.motion?.label || '';
    return {
      shot: item?.shot?.label || '',
      move: move.split(' · ').filter(Boolean),
    };
  },

  matches(i) {
    if (!this.filter) return true;
    const values = this.values(i);
    return this.filter.kind === 'shot' ? values.shot === this.filter.value : values.move.includes(this.filter.value);
  },

  setFilter(kind, value) {
    const same = this.filter && this.filter.kind === kind && this.filter.value === value;
    this.filter = same ? null : {kind, value};
    this.applyFilter();
    this.paint();
  },

  applyFilter() {
    (state.cards || []).forEach((card, i) => {
      card.item.hidden = !this.matches(i);
    });
    if (state.cursor != null && state.cards?.[state.cursor]?.item.hidden) {
      const first = state.cards.findIndex(card => !card.item.hidden);
      if (first >= 0 && typeof Review !== 'undefined') Review.setCursor(first);
    }
  },

  open() {
    try {
      return localStorage.getItem(OVERVIEW_OPEN_KEY) !== '0';
    } catch {
      return true;
    }
  },

  paint() {
    const box = this.el('#overview');
    if (!box) return;
    const spans = Analysis.shots || [];
    const summary = spans.some(Boolean) ? rhythmSummary(spans) : null;
    box.hidden = !summary;
    if (!summary) {
      if (this.filter) {
        this.filter = null;
        this.applyFilter();
      }
      return;
    }
    // The filter may point at values that no longer exist after a new analysis.
    if (this.filter && !(state.cards || []).some((_, i) => this.matches(i))) this.filter = null;
    this.applyFilter();
    box.open = this.open();
    this.el('#overviewHeadline').textContent = summary.headline;
    const body = this.el('#overviewBody');
    body.innerHTML = '';
    body.append(this.timeline(spans), this.bars(spans, summary.average));
    const notes = document.createElement('ul');
    notes.className = 'ovNotes';
    for (const text of summary.notes) {
      const line = document.createElement('li');
      line.textContent = text;
      notes.appendChild(line);
    }
    body.appendChild(notes);
    const count = state.cuts.length;
    const sizes = tally(state.cuts.map((_, i) => this.values(i).shot));
    const moves = tally(state.cuts.flatMap((_, i) => this.values(i).move));
    if (sizes.length) body.appendChild(this.chips('景别', 'shot', sizes, count));
    if (moves.length) body.appendChild(this.chips('运镜', 'move', moves, count));
    if (!sizes.length && !moves.length) {
      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = '点「分析画面」后，这里会统计景别和运镜，并可按它们筛选。';
      body.appendChild(hint);
    }
    const banner = this.el('#overviewFilter');
    const shown = (state.cards || []).filter(card => !card.item.hidden).length;
    banner.hidden = !this.filter;
    if (this.filter) {
      banner.querySelector('span').textContent = `正在只看「${this.filter.value}」：${shown} / ${count} 张`;
    }
  },

  // One segment per shot, as wide as the shot lasts, in the shot's main colour.
  timeline(spans) {
    const order = spans.map((span, i) => [span, i]).filter(([span]) => span);
    order.sort((a, b) => a[0].start - b[0].start);
    const total = order.reduce((sum, [span]) => sum + span.duration, 0) || 1;
    const row = document.createElement('div');
    row.className = 'ovTimeline';
    row.setAttribute('role', 'img');
    row.setAttribute('aria-label', '整片色带：每段是一个镜头，宽度是时长，颜色是主色');
    for (const [span, i] of order) {
      const segment = document.createElement('button');
      segment.type = 'button';
      segment.className = 'ovSegment';
      segment.style.flexGrow = String(span.duration / total);
      const colour = Analysis.items[i]?.color?.palette?.[0]?.hex;
      segment.style.background = colour || '#d6d9e4';
      segment.classList.toggle('dim', !this.matches(i));
      this.hover(segment, i, span);
      row.appendChild(segment);
    }
    return this.figure('整片色带（每段一个镜头，宽度为时长）', row);
  },

  // Shot lengths in order, with the average as a dashed line.
  bars(spans, average) {
    const order = spans.map((span, i) => [span, i]).filter(([span]) => span);
    order.sort((a, b) => a[0].start - b[0].start);
    const longest = Math.max(...order.map(([span]) => span.duration), 0.1);
    const chart = document.createElement('div');
    chart.className = 'ovBars';
    chart.setAttribute('role', 'img');
    chart.setAttribute('aria-label', `镜头时长：最长 ${longest.toFixed(1)} 秒，平均 ${average.toFixed(1)} 秒`);
    for (const [span, i] of order) {
      const bar = document.createElement('button');
      bar.type = 'button';
      bar.className = 'ovBar';
      bar.style.height = `${Math.max(3, (span.duration / longest) * 100)}%`;
      bar.classList.toggle('dim', !this.matches(i));
      this.hover(bar, i, span);
      chart.appendChild(bar);
    }
    const line = document.createElement('div');
    line.className = 'ovAverage';
    line.style.bottom = `${(average / longest) * 100}%`;
    const label = document.createElement('span');
    label.textContent = `平均 ${average.toFixed(1)} 秒`;
    line.appendChild(label);
    chart.appendChild(line);
    return this.figure('镜头时长（按时间顺序）', chart);
  },

  figure(title, content) {
    const box = document.createElement('figure');
    box.className = 'ovFigure';
    const caption = document.createElement('figcaption');
    caption.textContent = title;
    box.append(caption, content);
    return box;
  },

  chips(title, kind, counts, total) {
    const row = document.createElement('div');
    row.className = 'ovChips';
    const name = document.createElement('b');
    name.textContent = title;
    row.appendChild(name);
    for (const [value, count] of counts) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ovChip';
      const active = this.filter && this.filter.kind === kind && this.filter.value === value;
      chip.setAttribute('aria-pressed', String(!!active));
      chip.textContent = `${value} ${count}`;
      chip.title = `${Math.round((count / total) * 100)}% 的截图 · 单击只看这一类，再次单击取消`;
      chip.addEventListener('click', () => this.setFilter(kind, value));
      row.appendChild(chip);
    }
    return row;
  },

  // Tooltip with the shot's details; clicking goes to its card.
  hover(node, i, span) {
    const values = this.values(i);
    const text = [
      `#${String(i + 1).padStart(3, '0')} · ${shotSummary(span).length}`,
      shotSummary(span).range,
      [values.shot, Analysis.shots[i]?.motion?.text].filter(Boolean).join(' · '),
    ]
      .filter(Boolean)
      .join('\n');
    node.setAttribute('aria-label', text.replace(/\n/g, '，'));
    node.addEventListener('mouseenter', () => this.tip(node, text));
    node.addEventListener('focus', () => this.tip(node, text));
    node.addEventListener('mouseleave', () => this.tip(null));
    node.addEventListener('blur', () => this.tip(null));
    node.addEventListener('click', () => {
      if (state.cards?.[i]?.item.hidden) this.setFilter(this.filter.kind, this.filter.value); // clear the filter
      if (typeof Review !== 'undefined') Review.setCursor(i);
      state.cards?.[i]?.item.scrollIntoView({behavior: 'smooth', block: 'center'});
    });
  },

  tip(node, text) {
    const tip = this.el('#overviewTip');
    if (!node) {
      tip.hidden = true;
      return;
    }
    tip.textContent = text;
    tip.hidden = false;
    const box = node.getBoundingClientRect();
    const host = this.el('#overview').getBoundingClientRect();
    tip.style.left = `${Math.min(host.width - 220, Math.max(0, box.left - host.left + box.width / 2 - 110))}px`;
    const above = box.top - host.top - tip.offsetHeight - 8;
    // Not enough room above (the first row): show it below instead of over the heading.
    tip.style.top = `${above >= 40 ? above : box.bottom - host.top + 8}px`;
  },
};

if (typeof document !== 'undefined' && document.querySelector('#overview')) {
  const box = document.querySelector('#overview');
  box.addEventListener('toggle', () => {
    try {
      localStorage.setItem(OVERVIEW_OPEN_KEY, box.open ? '1' : '0');
    } catch {
      /* 仅本次有效 */
    }
  });
  document.querySelector('#overviewClear').addEventListener('click', () => {
    Overview.filter = null;
    Overview.applyFilter();
    Overview.paint();
  });
}
