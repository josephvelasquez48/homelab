// Audio worklets for the phone page. The wire format both ways is 16 kHz
// signed 16-bit mono in 20 ms frames (320 samples); the AudioContext runs
// at whatever rate the sound card uses, so both directions resample here.
const WIRE_RATE = 16000;
const FRAME = 320;

// Mic -> Pi. The call is wideband (mSBC, 16 kHz), so band-limit to 7 kHz
// with a windowed-sinc low-pass before decimating: a plain box average
// rolls off the top of the band and folds everything above 8 kHz back
// into it. The filter only runs at output samples (16k x 63 taps/s).
const TAPS = 63;
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / WIRE_RATE;
    const fc = 7000 / sampleRate;
    this.h = new Float32Array(TAPS);
    let sum = 0;
    for (let i = 0; i < TAPS; i++) {
      const m = i - (TAPS - 1) / 2;
      const sinc = m === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * m) / (Math.PI * m);
      const blackman = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (TAPS - 1)) + 0.08 * Math.cos((4 * Math.PI * i) / (TAPS - 1));
      this.h[i] = sinc * blackman;
      sum += this.h[i];
    }
    for (let i = 0; i < TAPS; i++) this.h[i] /= sum;
    this.hist = new Float32Array(TAPS); this.hpos = 0;
    this.pos = 0;
    this.buf = new Int16Array(FRAME); this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.hist[this.hpos] = ch[i];
      this.hpos = (this.hpos + 1) % TAPS;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        let acc = 0;
        for (let k = 0, j = this.hpos; k < TAPS; k++, j = (j + 1) % TAPS) acc += this.h[k] * this.hist[j];
        const v = Math.max(-1, Math.min(1, acc));
        this.buf[this.n++] = v * 32767;
        if (this.n === FRAME) {
          this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
          this.buf = new Int16Array(FRAME); this.n = 0;
        }
      }
    }
    return true;
  }
}

// Pi -> speakers. A small jitter buffer: start once 60 ms is queued, and
// if more than 500 ms piles up (tab was throttled), skip ahead to 100 ms
// rather than let the call run permanently behind.
class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = new Float32Array(WIRE_RATE * 2);
    this.r = 0; this.w = 0; this.size = 0;
    this.frac = 0; this.primed = false;
    this.step = WIRE_RATE / sampleRate;
    this.port.onmessage = (e) => {
      const s = new Int16Array(e.data);
      const len = this.q.length;
      for (let i = 0; i < s.length; i++) {
        this.q[this.w] = s[i] / 32768;
        this.w = (this.w + 1) % len;
        if (this.size < len) this.size++; else this.r = (this.r + 1) % len;
      }
      if (this.size > WIRE_RATE / 2) {
        const drop = this.size - WIRE_RATE / 10;
        this.r = (this.r + drop) % len; this.size -= drop;
      }
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    const len = this.q.length;
    if (!this.primed && this.size >= WIRE_RATE * 0.06) this.primed = true;
    for (let i = 0; i < out.length; i++) {
      if (!this.primed || this.size < 2) { out[i] = 0; this.primed = false; continue; }
      const a = this.q[this.r], b = this.q[(this.r + 1) % len];
      out[i] = a + (b - a) * this.frac;
      this.frac += this.step;
      while (this.frac >= 1) { this.frac -= 1; this.r = (this.r + 1) % len; this.size--; }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}

registerProcessor("capture", Capture);
registerProcessor("player", Player);
