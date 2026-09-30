/* Faster review of results: keyboard shortcuts and hints for near-duplicate screenshots.
   Uses `state`, `setKept`, `openLb` and `closeLb` from app.js. */
const Review = {
  similarToken: 0,
  flagged: [],

  el(selector) {
    return document.querySelector(selector);
  },

  // Screenshots whose similarity to the previous one reaches the threshold (pure, for tests).
  flags(scores, threshold) {
    return scores.flatMap((score, i) => (score != null && score >= threshold ? [i] : []));
  },

  async loadSimilar(res) {
    const token = ++this.similarToken;
    this.flagged = [];
    this.paintSimilar({});
    if (!state.sid || !res?.run || !state.cuts.length) return;
    let data;
    try {
      data = await jsonRequest(`/api/similar/${encodeURIComponent(state.sid)}?run=${encodeURIComponent(res.run)}`);
    } catch {
      return;
    } // 提示只是辅助，读取失败不影响挑选
    if (token !== this.similarToken || state.resultRun !== res.run) return;
    this.flagged = this.flags(data.scores || [], data.threshold ?? 80);
    this.paintSimilar(Object.fromEntries(this.flagged.map(i => [i, data.scores[i]])));
  },

  paintSimilar(scores) {
    (state.cards || []).forEach((card, i) => {
      card.cap.querySelector('.simBadge')?.remove();
      if (scores[i] == null) return;
      const badge = document.createElement('span');
      badge.className = 'badge simBadge';
      badge.textContent = `与上一张相似 ${Math.round(scores[i])}%`;
      badge.title = '和前一张截图几乎相同，可能是误检或重复镜头';
      card.cap.appendChild(badge);
    });
    this.updateSkipButton();
  },

  updateSkipButton() {
    const button = this.el('#btnSkipSimilar');
    const pending = this.flagged.filter(i => state.sel.has(i));
    button.hidden = !this.flagged.length;
    button.disabled = !pending.length || state.busy || state.editBusy;
    button.textContent = pending.length ? `跳过相似画面 (${pending.length})` : '相似画面已跳过';
  },

  skipSimilar() {
    const pending = this.flagged.filter(i => state.sel.has(i));
    if (!pending.length) return;
    setKept(pending, false);
    this.updateSkipButton();
    toast(`已跳过 ${pending.length} 张与上一张几乎相同的画面；单击卡片可随时恢复。`);
  },

  // ------------------------------------------------------------------ keyboard
  columns() {
    const template = getComputedStyle(this.el('#grid')).gridTemplateColumns;
    return Math.max(1, template.split(' ').filter(Boolean).length);
  },

  setCursor(i) {
    const count = state.cards?.length || 0;
    if (!count) return;
    state.cursor = Math.max(0, Math.min(count - 1, i));
    state.cards.forEach((card, k) => card.item.classList.toggle('cursor', k === state.cursor));
    state.cards[state.cursor].item.scrollIntoView({block: 'nearest'});
  },

  blocked(event) {
    const target = event.target;
    if (event.ctrlKey || event.metaKey || event.altKey) return true;
    if (
      target &&
      (target.isContentEditable ||
        (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) && target.type !== 'checkbox'))
    )
      return true;
    // Other panels own the keyboard while they are open.
    return (
      ['#collagePanel', '#storageManager', '#dl'].some(selector => this.el(selector) && !this.el(selector).hidden) ||
      this.el('#cardResult').hidden
    );
  },

  onKey(event) {
    if (this.blocked(event)) return;
    const lightbox = !this.el('#lb').hidden;
    const count = state.cards?.length || 0;
    const key = event.key;
    if (lightbox) {
      if (key === 'ArrowRight' || key === 'ArrowLeft') {
        const next = Math.max(0, Math.min(count - 1, (state.lbIndex ?? 0) + (key === 'ArrowRight' ? 1 : -1)));
        openLb(next);
        this.setCursor(next);
        event.preventDefault();
      } else if (key === ' ') {
        state.cards[state.lbIndex]?.toggle();
        event.preventDefault();
      }
      return;
    }
    if (key === '?') {
      const help = this.el('#shortcutHelp');
      help.open = !help.open;
      event.preventDefault();
      return;
    }
    if (!count) return;
    const moves = {ArrowRight: 1, ArrowLeft: -1, ArrowDown: this.columns(), ArrowUp: -this.columns()};
    if (key in moves) {
      this.setCursor(state.cursor == null ? 0 : state.cursor + moves[key]);
      event.preventDefault();
      return;
    }
    if (state.cursor == null) return;
    if (key === ' ' || key === 'x' || key === 'X') {
      state.cards[state.cursor].toggle();
      event.preventDefault();
    } else if (key === 'Enter') {
      openLb(state.cursor);
      event.preventDefault();
    } else if ((key === 'e' || key === 'E') && !state.cards[state.cursor].edit.disabled) {
      state.cards[state.cursor].edit.click();
      event.preventDefault();
    }
  },
};

function onResultsRendered(res) {
  Review.loadSimilar(res);
  if (state.cursor != null) Review.setCursor(state.cursor);
}

if (typeof document !== 'undefined' && document.querySelector('#btnSkipSimilar')) {
  document.addEventListener('keydown', event => Review.onKey(event));
  document.querySelector('#btnSkipSimilar').addEventListener('click', () => Review.skipSimilar());
}
