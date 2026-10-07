// Downsamples any input (usually 48 kHz stereo) to 16 kHz mono Int16 and posts ~100 ms chunks.
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.phase = 0;
    this.acc = 0;
    this.count = 0;
    this.out = new Int16Array(1600);
    this.used = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channels = input.length;
    const frames = input[0].length;
    for (let i = 0; i < frames; i++) {
      let s = 0;
      for (let c = 0; c < channels; c++) s += input[c][i];
      this.acc += s / channels;
      this.count++;
      this.phase += 1;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.count));
        this.acc = 0;
        this.count = 0;
        this.out[this.used++] = v * 32767;
        if (this.used === this.out.length) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]);
          this.out = new Int16Array(1600);
          this.used = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-tap", PcmTap);
