/* Corrections: when the analysis looks wrong, pick what it really is (every choice explains itself,
   and "说不清" is always allowed) or describe it in your own words. Saved automatically with the
   measurements of the moment, and exported as a JSON file (numbers and text only, no pictures) to
   calibrate the analysis. Uses `state`, `jsonRequest`, `toast` (app.js) and `Analysis`. */
const FIX_OK = 'ok';
const FIX_UNSURE = 'unsure';

// The current reading of a shot's camera movement, in the words of the "稳不稳" and "速度" choices.
function motionReading(move) {
  if (!move || !move.label) return {steady: '', speed: ''};
  const text = move.text || '';
  return {
    steady: text.includes('明显晃动') ? '明显晃动' : text.includes('轻微晃动') ? '稳，有轻微浮动' : '很稳',
    speed: ['固定', '手持'].includes(move.label)
      ? '没有移动'
      : text.startsWith('缓慢')
        ? '缓慢'
        : text.startsWith('快速')
          ? '快速'
          : '正常',
  };
}

// Single choices: [value, what it looks like]. Grouped by what they are about.
const FIX_FIELDS = [
  {
    key: 'shot',
    group: 'picture',
    title: '景别',
    help: '以主要人物为准，看他在画面里露出多少。',
    current: (item, move) => item?.shot?.label || (item ? '无法判断' : ''),
    options: [
      ['大特写', '只拍到脸的一部分，或眼睛、手等局部'],
      ['特写', '一张脸占满画面，到肩膀以上'],
      ['近景', '胸口以上'],
      ['中景', '腰部到膝盖以上'],
      ['全景', '从头到脚都在画面里，周围有一些环境'],
      ['远景', '人很小，主要在交代环境'],
      ['空镜', '画面里没有人：风景、物品、建筑'],
    ],
  },
  {
    key: 'composition',
    group: 'picture',
    title: '构图',
    current: item => item?.composition?.label || (item ? '无明显主体' : ''),
    options: [
      ['三分法构图', '主体在画面约三分之一的位置'],
      ['中心构图', '主体在正中间'],
      ['对称构图', '左右或上下像照镜子一样'],
      ['偏侧构图', '主体靠一边，另一边留出空白'],
      ['左右平衡构图', '两个人或两样东西一左一右'],
      ['引导线构图', '道路、栏杆等线条把视线引向主体'],
      ['框架构图', '透过门、窗、树枝等「框」看主体'],
      ['其他', '都不像，可以在下面描述'],
    ],
  },
  {
    key: 'depth',
    group: 'picture',
    title: '景深',
    current: item => item?.depth?.label || '',
    options: [
      ['浅景深', '主体清楚，背景明显模糊'],
      ['深景深', '前后都清楚'],
      ['整体偏糊', '整张图都不清楚：运动模糊或失焦'],
    ],
  },
  {
    key: 'steady',
    group: 'shot',
    title: '机器稳不稳',
    current: (item, move) => motionReading(move).steady,
    options: [
      ['很稳', '三脚架、滑轨：画面纹丝不动或非常顺滑'],
      ['稳，有轻微浮动', '稳定器、云台：在走动，但不抖'],
      ['明显晃动', '手持：能看出抖动'],
    ],
  },
  {
    key: 'speed',
    group: 'shot',
    title: '运镜速度',
    current: (item, move) => motionReading(move).speed,
    options: [
      ['没有移动', '机器没动'],
      ['缓慢', '慢慢地动，不留意几乎看不出'],
      ['正常', '看得出在动，不急不慢'],
      ['快速', '动得很快，甚至有甩的感觉'],
    ],
  },
  {
    key: 'cut',
    group: 'shot',
    title: '镜头切分',
    help: '按 1 / 2 / 3 看这个镜头的首帧、中间、尾帧。',
    current: (item, move, span) => (span ? '按检测结果' : ''),
    options: [
      ['多切了', '这里其实没换镜头，和上一张是同一个镜头'],
      ['漏切了', '这个镜头中间其实还换过镜头'],
      ['切点不准', '确实换了镜头，但时间点偏早或偏晚'],
    ],
  },
];

// Camera movements, any number of them: [value, what it looks like].
const FIX_MOVES = [
  ['固定', '机器不动（画面里的人可以动）'],
  ['推近', '画面里的东西越来越大，像走近了'],
  ['拉远', '画面里的东西越来越小，像退后了'],
  ['左摇/移', '画面往左转，或整台机器往左移'],
  ['右摇/移', '画面往右转，或整台机器往右移'],
  ['上摇/升', '镜头往上抬，或机器升高'],
  ['下摇/降', '镜头往下压，或机器降低'],
  ['旋转', '画面歪着转'],
  ['跟拍', '跟着人走，人一直在画面里差不多的位置'],
  ['环绕', '绕着人或物转'],
  ['甩镜头', '很快地甩过去，中间画面糊掉'],
  ['变焦', '机器没动，靠镜头放大缩小（远近的透视不变）'],
];

// Short text for a saved correction, e.g. "景别 → 特写 · 运镜 → 推近". Pure, so it can be tested.
function fixSummary(fix) {
  if (!fix) return '';
  const word = value => (value === FIX_OK ? '✓ 对' : value === FIX_UNSURE ? '说不清' : value);
  const parts = FIX_FIELDS.filter(field => fix[field.key]).map(field => `${field.title} → ${word(fix[field.key])}`);
  if (fix.moves?.length) parts.push(`运镜 → ${fix.moves.map(word).join('、')}`);
  if (fix.note) parts.push('有描述');
  return parts.join(' · ');
}

const Corrections = {
  items: [], // per card: the saved correction or null
  total: 0, // corrections in this whole record, all results
  open: false, // the form stays open from picture to picture once opened
  pending: null, // {i, fix, timer}: a typed description waiting to be saved
  queue: Promise.resolve(),

  el(selector) {
    return document.querySelector(selector);
  },

  accept(data) {
    this.items = data.corrections || [];
    this.total = data.corrections_total || 0;
    this.paintExport();
  },

  exportUrl() {
    return `/api/corrections/${encodeURIComponent(state.sid)}/export`;
  },

  // ------------------------------------------------------------------ saving
  save(i, fix) {
    const run = Analysis.run;
    const sid = state.sid;
    this.queue = this.queue.then(async () => {
      try {
        const data = await jsonRequest(`/api/corrections/${encodeURIComponent(sid)}`, {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({run, index: i, fix}),
        });
        if (sid !== state.sid || run !== Analysis.run) return;
        this.items[i] = data.fix;
        this.total = data.total;
        this.paintCard(i);
        this.paintExport();
        this.paintState(i, '已保存');
      } catch (e) {
        this.paintState(i, '保存失败', true);
        toast(e.message || '纠错保存失败', true);
      }
    });
    return this.queue;
  },

  // Save a typed description a moment after the last key, or now when leaving the picture.
  later(i, fix) {
    clearTimeout(this.pending?.timer);
    this.pending = {i, fix, timer: setTimeout(() => this.flush(), 700)};
    this.paintState(i, '正在输入…');
  },

  flush() {
    if (!this.pending) return;
    const {i, fix, timer} = this.pending;
    clearTimeout(timer);
    this.pending = null;
    this.save(i, fix);
  },

  collect(form) {
    const fix = {};
    for (const select of form.querySelectorAll('select[name]')) if (select.value) fix[select.name] = select.value;
    const moves = [...form.querySelectorAll('input[name="moves"]:checked')].map(box => box.value);
    if (moves.length) fix.moves = moves;
    const note = form.querySelector('textarea').value.trim();
    if (note) fix.note = note;
    return fix;
  },

  // ------------------------------------------------------------------ painting
  paintCard(i) {
    const card = state.cards?.[i];
    if (!card) return;
    card.cap.querySelector('.fixMark')?.remove();
    const fix = this.items[i];
    if (!fix) return;
    const mark = document.createElement('span');
    mark.className = 'fixMark';
    mark.textContent = '✎';
    mark.title = `已纠错：${fixSummary(fix)}`;
    card.cap.appendChild(mark);
  },

  paintExport() {
    const link = this.el('#btnFixExport');
    if (!link) return;
    link.hidden = !this.total;
    link.textContent = `导出纠错记录（${this.total}）`;
    link.href = state.sid ? this.exportUrl() : '';
  },

  paintState(i, text, bad = false) {
    const line = this.el('#fixState');
    if (!line || Number(line.dataset.index) !== i) return;
    line.textContent = text;
    line.classList.toggle('bad', bad);
    const summary = this.el('#fixSummary');
    if (summary) summary.textContent = fixSummary(this.items[i]) || '觉得分析不对？点这里纠正';
  },

  toggle() {
    if (typeof Viewer === 'undefined' || !Viewer.isOpen()) return;
    const box = this.el('.fixBox');
    if (!box) return;
    box.open = !box.open;
    if (box.open) {
      box.scrollIntoView({block: 'nearest'});
      // Focus the heading, not a choice: the arrows keep paging instead of changing an answer.
      box.querySelector('summary')?.focus({preventScroll: true});
    }
  },

  select(field, value, reading) {
    const label = document.createElement('label');
    label.className = 'fixField';
    const head = document.createElement('span');
    head.className = 'fixHead';
    const title = document.createElement('b');
    title.textContent = field.title;
    const now = document.createElement('small');
    now.textContent = reading ? `分析：${reading}` : '尚未分析';
    head.append(title, now);
    const select = document.createElement('select');
    select.name = field.key;
    const add = (optionValue, text) => {
      const option = document.createElement('option');
      option.value = optionValue;
      option.textContent = text;
      select.appendChild(option);
    };
    add('', '— 不改');
    if (reading) add(FIX_OK, '✓ 分析对了');
    for (const [name, hint] of field.options) add(name, `${name} · ${hint}`);
    add(FIX_UNSURE, '？ 说不清');
    // A value saved earlier that is no longer in the list still shows.
    if (value && ![...select.options].some(option => option.value === value)) add(value, value);
    select.value = value || '';
    label.append(head, select);
    if (field.help) {
      const help = document.createElement('small');
      help.className = 'fixHelp';
      help.textContent = field.help;
      label.appendChild(help);
    }
    return label;
  },

  moveChips(saved, reading) {
    const box = document.createElement('fieldset');
    box.className = 'fixField fixMoves';
    const legend = document.createElement('legend');
    legend.className = 'fixHead';
    const title = document.createElement('b');
    title.textContent = '运镜（可多选）';
    const now = document.createElement('small');
    now.textContent = reading ? `分析：${reading}` : '尚未分析';
    legend.append(title, now);
    box.appendChild(legend);
    const chips = [...(reading ? [[FIX_OK, '分析对了']] : []), ...FIX_MOVES, [FIX_UNSURE, '看不出是哪种']];
    for (const [value, hint] of chips) {
      const chip = document.createElement('label');
      chip.className = 'fixChip';
      chip.title = hint;
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'moves';
      input.value = value;
      input.checked = saved.includes(value);
      const text = document.createElement('span');
      text.textContent = value === FIX_OK ? '✓ 对了' : value === FIX_UNSURE ? '？ 说不清' : value;
      chip.append(input, text);
      box.appendChild(chip);
    }
    // "对了" and "说不清" stand alone; picking a movement clears them, and the other way round.
    box.addEventListener('change', event => {
      const picked = event.target;
      if (!picked.checked) return;
      const alone = [FIX_OK, FIX_UNSURE].includes(picked.value);
      for (const other of box.querySelectorAll('input')) {
        if (other !== picked && (alone || [FIX_OK, FIX_UNSURE].includes(other.value))) other.checked = false;
      }
    });
    const help = document.createElement('small');
    help.className = 'fixHelp';
    help.textContent = '鼠标停在选项上可以看说明。摇和移分不清时，选同一个方向就行。';
    box.appendChild(help);
    return box;
  },

  // The correction form at the top of the viewer panel, for picture i.
  section(i, panel) {
    this.flush();
    const item = Analysis.items[i];
    const span = Analysis.shots[i];
    const move = span?.motion;
    const saved = this.items[i] || {};
    const box = document.createElement('details');
    box.className = 'fixBox';
    box.open = this.open;
    box.addEventListener('toggle', () => {
      this.open = box.open;
    });
    const summary = document.createElement('summary');
    const title = document.createElement('b');
    title.textContent = '✎ 纠错';
    const text = document.createElement('span');
    text.id = 'fixSummary';
    text.textContent = fixSummary(this.items[i]) || '觉得分析不对？点这里纠正';
    summary.append(title, text);
    box.appendChild(summary);

    const form = document.createElement('form');
    form.className = 'fixForm';
    form.addEventListener('submit', event => event.preventDefault());
    const intro = document.createElement('p');
    intro.className = 'fixHelp';
    intro.textContent = '只改你看得出的项目，看不准就选「说不清」或直接写描述。改完自动保存。';
    form.appendChild(intro);
    const group = (name, fields) => {
      const heading = document.createElement('h5');
      heading.textContent = name;
      form.appendChild(heading);
      for (const field of fields)
        form.appendChild(this.select(field, saved[field.key], field.current(item, move, span)));
    };
    group(
      '这张图',
      FIX_FIELDS.filter(field => field.group === 'picture'),
    );
    if (span) {
      const heading = document.createElement('h5');
      heading.textContent = '这个镜头';
      form.appendChild(heading);
      form.appendChild(this.moveChips(saved.moves || [], move?.label || ''));
      for (const field of FIX_FIELDS.filter(f => f.group === 'shot'))
        form.appendChild(this.select(field, saved[field.key], field.current(item, move, span)));
    }
    const note = document.createElement('label');
    note.className = 'fixField';
    const noteHead = document.createElement('span');
    noteHead.className = 'fixHead';
    const noteTitle = document.createElement('b');
    noteTitle.textContent = '自己描述（可选）';
    noteHead.appendChild(noteTitle);
    const area = document.createElement('textarea');
    area.rows = 3;
    area.maxLength = 2000;
    area.placeholder = '用自己的话说说哪里不对，比如：人从左走到右，镜头跟着往右移；其实是逆光。';
    area.value = saved.note || '';
    note.append(noteHead, area);
    form.appendChild(note);

    const foot = document.createElement('div');
    foot.className = 'fixFoot';
    const status = document.createElement('span');
    status.id = 'fixState';
    status.className = 'fixStateText';
    status.dataset.index = String(i);
    status.textContent = this.items[i] ? '已保存' : '';
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'vwBtn';
    clear.textContent = '清除这张的纠错';
    clear.disabled = !this.items[i];
    clear.addEventListener('click', () => {
      for (const select of form.querySelectorAll('select')) select.value = '';
      for (const input of form.querySelectorAll('input')) input.checked = false;
      area.value = '';
      clear.disabled = true;
      clearTimeout(this.pending?.timer);
      this.pending = null;
      this.save(i, {});
    });
    foot.append(status, clear);
    form.appendChild(foot);

    form.addEventListener('change', event => {
      if (event.target === area) return;
      // Hand the keyboard back to the viewer: the arrows page on instead of changing the choice.
      event.target.blur();
      clear.disabled = false;
      clearTimeout(this.pending?.timer);
      this.pending = null;
      this.save(i, this.collect(form));
    });
    area.addEventListener('input', () => {
      clear.disabled = false;
      this.later(i, this.collect(form));
    });
    area.addEventListener('blur', () => this.flush());
    box.appendChild(form);
    panel.appendChild(box);
  },
};

if (typeof document !== 'undefined' && document.querySelector('#btnFixExport')) {
  if (typeof Confirm !== 'undefined')
    Confirm.guardLink(document.querySelector('#btnFixExport'), () => ({
      kind: 'download',
      title: '导出纠错记录？',
      message: `共 ${Corrections.total} 条。文件里只有测量数值和你的纠错、描述，不含任何图片，可以直接发给开发者用来校准。`,
      ok: '导出',
    }));
  window.addEventListener('beforeunload', () => Corrections.flush());
}
