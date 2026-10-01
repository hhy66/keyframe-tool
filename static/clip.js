/* Shot clip: the shot itself, played from the original video in the viewer and looping between its
   first and last frame. Slow motion and frame steps make the camera movement easy to see. Nothing is
   cut or re-encoded: the browser seeks in the original file. Uses `state`, `toast` (app.js), `Viewer`
   and `Analysis` (the shots). */
const CLIP_SPEED_KEY = 'keyframe-tool.viewer.clipSpeed';
const CLIP_MUTED_KEY = 'keyframe-tool.viewer.clipMuted';
const CLIP_SPEEDS = [0.25, 0.5, 1];
const CLIP_VIEW = 4; // Analysis.viewing while the clip plays

// The frames of a shot on the video's clock. Pure, so it can be tested on its own.
function clipFrames(span) {
  const count = Math.max(1, span.frames);
  const step = span.duration / count;
  const clamp = k => Math.max(0, Math.min(count - 1, k));
  return {
    count,
    step,
    // A moment well inside frame k, so the browser never lands on the frame before it.
    at: k => span.start + (clamp(k) + 0.3) * step,
    // The frame shown at a moment of the video.
    index: time => clamp(Math.floor((time - span.start) / step + 1e-6)),
    // Where the first / middle / last frames sit on the clip's progress bar (0–1).
    marks: span.strip.map(frame => (Math.max(0, Math.min(count - 1, frame - span.start_frame)) + 0.5) / count),
  };
}

const Clip = {
  span: null,
  frames: null,
  playing: false, // what the user asked for; the loop pauses briefly on its own
  failed: false, // the browser cannot play this video
  rewind: null, // timer that jumps back to the first frame; -1 while the last frame is being sought
  token: 0, // bumps when the clip changes, ending the previous frame watcher
  raf: 0,

  el(selector) {
    return document.querySelector(selector);
  },
  video() {
    return this.el('#vwClip');
  },
  active() {
    return this.span != null;
  },
  speed() {
    try {
      const value = Number(localStorage.getItem(CLIP_SPEED_KEY));
      return CLIP_SPEEDS.includes(value) ? value : 1;
    } catch {
      return 1;
    }
  },
  muted() {
    try {
      return localStorage.getItem(CLIP_MUTED_KEY) !== '0';
    } catch {
      return true;
    }
  },
  remember(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* 仅本次有效 */
    }
  },

  // Play shot i in place of the picture. Returns false (and says why unless quiet) when it cannot.
  show(i, quiet = false) {
    const span = Analysis.shots[i];
    const say = text => quiet || toast(text, true);
    if (!span) return (say('这张截图没有镜头起止信息，无法播放片段'), false);
    if (!Analysis.video) return (say('原视频已清理，无法播放镜头片段'), false);
    const video = this.video();
    const src = `/api/video/${encodeURIComponent(state.sid)}`;
    if (video.dataset.src !== src) {
      this.failed = false;
      video.dataset.src = src;
      video.src = src;
    }
    if (this.failed) return (say('浏览器播放不了这个视频格式，暂时只能看首 / 中 / 尾帧'), false);
    this.stopTimers();
    this.span = span;
    this.frames = clipFrames(span);
    video.playbackRate = this.speed();
    video.muted = this.muted();
    video.hidden = false;
    this.el('#vwImg').hidden = true;
    this.el('#vwClipBar').hidden = false;
    this.el('#lb').classList.add('clipping');
    this.seek(0);
    // A shot of one or two frames is shown, not looped.
    this.playing = this.frames.count > 2;
    if (this.playing) this.play();
    else video.pause();
    this.watch();
    this.paintBar();
    Viewer.fit();
    return true;
  },

  hide() {
    if (!this.active()) return;
    this.stopTimers();
    this.token += 1;
    this.span = this.frames = null;
    this.playing = false;
    const video = this.video();
    video.pause();
    video.hidden = true;
    this.el('#vwImg').hidden = false;
    this.el('#vwClipBar').hidden = true;
    this.el('#lb').classList.remove('clipping');
  },

  // Leaving the viewer: stop downloading the video so the record is not held open.
  release() {
    this.hide();
    const video = this.video();
    if (!video?.dataset.src) return;
    video.removeAttribute('src');
    delete video.dataset.src;
    video.load();
  },

  stopTimers() {
    clearTimeout(this.rewind);
    this.rewind = null;
    cancelAnimationFrame(this.raf);
    this.raf = 0;
  },

  play() {
    const video = this.video();
    video.play().catch(() => {
      /* 暂停或切走时 play() 会被打断，不算错误 */
    });
  },

  seek(k) {
    this.video().currentTime = this.frames.at(k);
  },

  toggle() {
    if (!this.active() || this.frames.count <= 2) return;
    this.playing = !this.playing;
    clearTimeout(this.rewind);
    this.rewind = null;
    const video = this.video();
    if (this.playing) {
      if (this.frames.index(video.currentTime) >= this.frames.count - 2) this.seek(0);
      this.play();
    } else video.pause();
    this.paintBar();
  },

  // One frame back or forward; pauses so the frame stays on screen.
  step(delta) {
    if (!this.active()) return;
    this.playing = false;
    clearTimeout(this.rewind);
    this.rewind = null;
    const video = this.video();
    video.pause();
    this.seek(this.frames.index(video.currentTime) + delta);
    this.paintBar();
  },

  setSpeed(value) {
    this.remember(CLIP_SPEED_KEY, String(value));
    if (this.active()) this.video().playbackRate = value;
    this.paintBar();
  },

  toggleSound() {
    const muted = !this.muted();
    this.remember(CLIP_MUTED_KEY, muted ? '1' : '0');
    this.video().muted = muted;
    this.paintBar();
  },

  // Watch the playing frame. On the second-to-last frame, stop and put the last frame up by seeking:
  // playing on would decode the next shot and flash it. After one frame, start over.
  watch() {
    const video = this.video();
    const check = time => {
      if (!this.active() || !this.playing || this.rewind || video.paused) return;
      if (this.frames.index(time) < this.frames.count - 2) return;
      video.pause();
      this.seek(this.frames.count - 1);
      // Hold the last frame for one frame once it is on screen (seeking can take a moment).
      this.rewind = -1;
      video.addEventListener(
        'seeked',
        () => {
          if (this.rewind !== -1 || !this.active() || !this.playing) return;
          this.rewind = setTimeout(
            () => {
              this.rewind = null;
              if (!this.active() || !this.playing) return;
              this.seek(0);
              this.play();
            },
            (this.frames.step / video.playbackRate) * 1000,
          );
        },
        {once: true},
      );
    };
    if ('requestVideoFrameCallback' in video) {
      const token = ++this.token;
      const onFrame = (now, meta) => {
        if (this.token !== token || !this.active()) return;
        check(meta.mediaTime);
        video.requestVideoFrameCallback(onFrame);
      };
      video.requestVideoFrameCallback(onFrame);
    }
    const tick = () => {
      if (!this.active()) return;
      if (!('requestVideoFrameCallback' in video)) check(video.currentTime);
      this.paintProgress();
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  },

  // Drag or click on the bar to go to that frame.
  scrub(event) {
    if (!this.active()) return;
    const track = this.el('#vwClipTrack');
    const box = track.getBoundingClientRect();
    const share = Math.max(0, Math.min(0.9999, (event.clientX - box.left) / box.width));
    const video = this.video();
    if (this.playing) {
      this.playing = false;
      video.pause();
    }
    clearTimeout(this.rewind);
    this.rewind = null;
    this.seek(Math.floor(share * this.frames.count));
    this.paintBar();
  },

  // ------------------------------------------------------------------ painting
  paintBar() {
    if (!this.active()) return;
    const play = this.el('#vwClipPlay');
    play.textContent = this.playing ? '❚❚' : '▶';
    play.setAttribute('aria-label', this.playing ? '暂停（K）' : '播放（K）');
    play.title = play.getAttribute('aria-label');
    play.disabled = this.frames.count <= 2;
    const speed = this.speed();
    for (const button of document.querySelectorAll('#vwClipSpeeds button')) {
      button.setAttribute('aria-pressed', String(Number(button.dataset.speed) === speed));
    }
    const sound = this.el('#vwClipSound');
    const muted = this.muted();
    sound.textContent = muted ? '声音：关' : '声音：开';
    sound.setAttribute('aria-pressed', String(!muted));
    const marks = this.el('#vwClipMarks');
    marks.innerHTML = '';
    ['首', '中', '尾'].forEach((name, k) => {
      const mark = document.createElement('i');
      mark.style.left = `${this.frames.marks[k] * 100}%`;
      mark.title = `${name}帧`;
      mark.dataset.name = name;
      marks.appendChild(mark);
    });
    this.paintProgress();
  },

  paintProgress() {
    if (!this.active()) return;
    const time = this.video().currentTime;
    const k = this.frames.index(time);
    const count = this.frames.count;
    this.el('#vwClipFill').style.width = `${((k + 1) / count) * 100}%`;
    const seconds = Math.max(0, time - this.span.start);
    this.el('#vwClipTime').textContent =
      `第 ${k + 1} / ${count} 帧 · ${seconds.toFixed(1)} / ${this.span.duration.toFixed(1)} 秒`;
  },
};

if (typeof document !== 'undefined' && document.querySelector('#vwClip')) {
  const video = document.querySelector('#vwClip');
  video.addEventListener('loadedmetadata', () => {
    if (Clip.active()) Viewer.fit();
  });
  video.addEventListener('error', () => {
    if (!video.dataset.src) return;
    Clip.failed = true;
    if (!Clip.active()) return;
    Analysis.viewFrame(0);
    toast('浏览器播放不了这个视频格式，暂时只能看首 / 中 / 尾帧', true);
  });
  video.addEventListener('click', () => Clip.toggle());
  document.querySelector('#vwClipPlay').addEventListener('click', () => Clip.toggle());
  document.querySelector('#vwClipPrev').addEventListener('click', () => Clip.step(-1));
  document.querySelector('#vwClipNext').addEventListener('click', () => Clip.step(1));
  document.querySelector('#vwClipSound').addEventListener('click', () => Clip.toggleSound());
  document.querySelector('#vwClipBack').addEventListener('click', () => Analysis.viewFrame(0));
  for (const button of document.querySelectorAll('#vwClipSpeeds button')) {
    button.addEventListener('click', () => Clip.setSpeed(Number(button.dataset.speed)));
  }
  // Dragging on the controls must not count as a swipe to the next picture.
  document.querySelector('#vwClipBar').addEventListener('pointerdown', event => event.stopPropagation());
  const track = document.querySelector('#vwClipTrack');
  track.addEventListener('pointerdown', event => {
    track.setPointerCapture(event.pointerId);
    Clip.scrub(event);
  });
  track.addEventListener('pointermove', event => {
    if (track.hasPointerCapture(event.pointerId)) Clip.scrub(event);
  });
}
