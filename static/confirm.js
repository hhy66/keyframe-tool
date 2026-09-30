/* Second confirmation before downloads and analysis runs, so a stray click does nothing.
   Each kind of action can be waved through for the rest of this page visit. */
const Confirm = {
  skipped: new Set(),
  pending: null,
  box: null,

  el(selector) {
    return document.querySelector(selector);
  },
  isOpen() {
    return !!this.box && !this.box.hidden;
  },

  build() {
    if (this.box) return;
    const box = document.createElement('div');
    box.className = 'modal confirmModal';
    box.id = 'confirmDialog';
    box.hidden = true;
    box.setAttribute('role', 'alertdialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'confirmTitle');
    box.setAttribute('aria-describedby', 'confirmMessage');
    box.innerHTML = `
      <div class="confirmBox">
        <h2 id="confirmTitle"></h2>
        <p id="confirmMessage"></p>
        <ul id="confirmDetails"></ul>
        <label class="confirmSkip"><input type="checkbox" id="confirmSkip"> <span>本次打开期间不再询问这类操作</span></label>
        <div class="confirmActions">
          <button type="button" class="btn ghost" id="confirmCancel">取消</button>
          <button type="button" class="btn" id="confirmOk">确定</button>
        </div>
      </div>`;
    document.body.appendChild(box);
    this.box = box;
    this.el('#confirmOk').addEventListener('click', () => this.finish(true));
    this.el('#confirmCancel').addEventListener('click', () => this.finish(false));
    box.addEventListener('click', event => {
      if (event.target === box) this.finish(false);
    });
    // Window capture runs before every other shortcut handler, so keys never leak to the page behind.
    window.addEventListener('keydown', event => this.onKey(event), true);
  },

  // Ask before `kind` of action; resolves true to go ahead. Kinds waved through this visit resolve at once.
  ask({kind, title, message = '', details = [], ok = '确定', cancel = '取消'}) {
    if (kind && this.skipped.has(kind)) return Promise.resolve(true);
    if (this.pending) return Promise.resolve(false); // one question at a time; a double click asks once
    this.build();
    this.el('#confirmTitle').textContent = title;
    this.el('#confirmMessage').textContent = message;
    this.el('#confirmMessage').hidden = !message;
    const list = this.el('#confirmDetails');
    list.innerHTML = '';
    for (const line of details.filter(Boolean)) {
      const item = document.createElement('li');
      item.textContent = line;
      list.appendChild(item);
    }
    list.hidden = !list.children.length;
    this.el('#confirmOk').textContent = ok;
    this.el('#confirmCancel').textContent = cancel;
    this.el('#confirmSkip').checked = false;
    this.el('#confirmSkip').parentElement.hidden = !kind;
    this.returnFocus = document.activeElement;
    this.box.hidden = false;
    this.el('#confirmOk').focus();
    return new Promise(resolve => {
      this.pending = {kind, resolve};
    });
  },

  finish(accepted) {
    if (!this.pending) return;
    const {kind, resolve} = this.pending;
    this.pending = null;
    if (accepted && kind && this.el('#confirmSkip').checked) this.skipped.add(kind);
    this.box.hidden = true;
    if (this.returnFocus?.isConnected) this.returnFocus.focus({preventScroll: true});
    resolve(accepted);
  },

  onKey(event) {
    if (!this.isOpen()) return;
    const buttons = [this.el('#confirmSkip'), this.el('#confirmCancel'), this.el('#confirmOk')].filter(
      node => !node.closest('[hidden]'),
    );
    // Nothing reaches the page behind while the dialog is open; Space keeps its native meaning.
    event.stopImmediatePropagation();
    if (event.key === 'Escape') this.finish(false);
    else if (event.key === 'Enter') this.finish(document.activeElement !== this.el('#confirmCancel'));
    else if (event.key === 'Tab') {
      // Keep focus inside the dialog.
      const at = buttons.indexOf(document.activeElement);
      const next = (at + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length;
      buttons[next].focus();
    } else return;
    event.preventDefault();
  },

  // A download link that asks first: the confirmed click is replayed so the browser still saves the file.
  guardLink(link, options) {
    link.addEventListener('click', async event => {
      if (link.dataset.confirmed === '1') {
        delete link.dataset.confirmed;
        return;
      }
      if (link.getAttribute('aria-disabled') === 'true' || !link.getAttribute('href')) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!(await this.ask(typeof options === 'function' ? options() : options))) return;
      link.dataset.confirmed = '1';
      link.click();
    });
  },
};
