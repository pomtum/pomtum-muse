export type Mood = 'idle' | 'listening' | 'thinking' | 'speaking' | 'offline';
export interface MouthShape { open: number; wide: number; round: number }
interface Sequence { file: string; frames: number; mode: number; level: number }
interface Manifest { width: number; height: number; tileWidth: number; columns: number; fps: number; sequences: Record<string, Sequence> }

const unit = (value: number) => Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;

/** Lossless frames emitted by the unchanged official ESP32 C renderer. */
export class Companion {
  mood: Mood = 'offline';
  amplitude = 0;
  // Kept for the audio/UI contract. The official renderer uses level, not phonemes.
  mouthShape: MouthShape = { open: 0, wide: 0, round: 0 };
  fps = 0;

  private readonly canvas = document.createElement('canvas');
  private readonly sample = document.createElement('canvas');
  private readonly context: CanvasRenderingContext2D;
  private readonly sampleContext: CanvasRenderingContext2D;
  private readonly images = new Map<string, HTMLImageElement>();
  private readonly loadingImages = new Set<HTMLImageElement>();
  private readonly controller = new AbortController();
  private readonly reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  private readonly observer: ResizeObserver;
  private manifest: Manifest | null = null;
  private disposed = false;
  private failed = false;
  private raf = 0;
  private lastFrame = 0;
  private lastRendered = 0;
  private time = 0;
  private phaseStart = 0;
  private previousMood: Mood = 'offline';
  private petStart = -100;
  private fpsStart = 0;
  private frameCount = 0;
  private size = 64;
  private map = new Uint8Array(64);
  private pixels: ImageData;
  private lastPaint = '';

  constructor(private readonly host: HTMLElement) {
    const context = this.canvas.getContext('2d', { alpha: false });
    const sampleContext = this.sample.getContext('2d', { willReadFrequently: true, alpha: false });
    if (!context || !sampleContext) throw new Error('Canvas renderer unavailable');
    this.context = context;
    this.sampleContext = sampleContext;
    this.sample.width = 128;
    this.sample.height = 64;
    this.sampleContext.imageSmoothingEnabled = false;
    this.canvas.setAttribute('aria-hidden', 'true');
    Object.assign(this.canvas.style, {
      display: 'block', position: 'absolute', left: '50%', top: '50%',
      transform: 'translate(-50%, -50%)', imageRendering: 'pixelated',
    });
    this.pixels = this.context.createImageData(64, 64);
    host.append(this.canvas);
    this.observer = new ResizeObserver(this.resize);
    this.observer.observe(host);
    this.resize();
    this.host.addEventListener('click', this.onClick);
    document.addEventListener('visibilitychange', this.onVisibility);
    this.reducedMotion.addEventListener('change', this.onMotionPreference);
    void this.load().catch(() => {
      if (!this.disposed) {
        this.failed = true;
        cancelAnimationFrame(this.raf);
        this.fps = 0;
        this.host.dispatchEvent(new CustomEvent('renderlost'));
      }
    });
  }

  private async load() {
    const base = '/esp32-avatar/';
    const response = await fetch(base + 'manifest.json', { signal: this.controller.signal });
    if (!response.ok) throw new Error('Official avatar manifest unavailable');
    const manifest: Manifest = await response.json();
    if (manifest.width !== 64 || manifest.height !== 64 || manifest.tileWidth !== 128 || manifest.columns !== 16) {
      throw new Error('Unexpected official avatar atlas');
    }
    this.manifest = manifest;
    const loadImage = async (name: string) => {
      const image = new Image();
      this.loadingImages.add(image);
      image.src = base + manifest.sequences[name].file;
      try {
        await image.decode();
        if (!this.disposed) this.images.set(name, image);
      } finally {
        this.loadingImages.delete(image);
      }
    };
    await loadImage('idle');
    if (this.disposed) return;
    this.onVisibility();
    await Promise.all(Object.keys(manifest.sequences).filter(name => name !== 'idle').map(loadImage));
  }

  private readonly resize = () => {
    if (this.disposed) return;
    // Match muse_pixel_set_size(): at most 512 pixels and a 64-cell hard grid.
    this.size = Math.max(1, Math.min(512, Math.floor(Math.min(this.host.clientWidth, this.host.clientHeight))));
    this.canvas.width = this.canvas.height = this.size;
    this.canvas.style.width = this.canvas.style.height = this.size + 'px';
    this.context.imageSmoothingEnabled = false;
    this.pixels = this.context.createImageData(this.size, this.size);
    this.map = new Uint8Array(this.size);
    const grid = this.size >= 192;
    for (let i = 0; i < this.size; i++) {
      const cell = Math.floor(i * 64 / this.size);
      const edge = grid && Math.floor((i + 1) * 64 / this.size) !== cell;
      this.map[i] = cell | (edge ? 128 : 0);
    }
    this.lastPaint = '';
  };

  private readonly onClick = () => {
    if (!this.reducedMotion.matches) this.petStart = this.time;
  };

  private readonly onMotionPreference = () => {
    this.petStart = -100;
    this.lastPaint = '';
  };

  private readonly onVisibility = () => {
    cancelAnimationFrame(this.raf);
    this.lastFrame = this.lastRendered = this.fpsStart = this.frameCount = this.fps = 0;
    if (!this.disposed && !this.failed && !document.hidden && this.images.has('idle')) {
      this.raf = requestAnimationFrame(this.draw);
    }
  };

  private readonly draw = (now: number) => {
    if (this.disposed || this.failed || document.hidden || !this.manifest) return;
    this.raf = requestAnimationFrame(this.draw);
    const interval = 1000 / this.manifest.fps;
    const elapsed = this.lastFrame ? now - this.lastFrame : interval;
    if (elapsed < interval) return;
    this.lastFrame = now - elapsed % interval;
    this.time += this.lastRendered ? Math.min((now - this.lastRendered) / 1000, .2) : interval / 1000;
    this.lastRendered = now;
    if (this.mood !== this.previousMood) {
      this.previousMood = this.mood;
      this.phaseStart = this.time;
    }
    const quiet = this.reducedMotion.matches;
    let phase = quiet ? 0 : this.time - this.phaseStart;
    let sequence: string = this.mood === 'offline' ? 'idle' : this.mood;
    if (sequence === 'speaking' || sequence === 'listening') {
      // Select an original C level sample; zero always selects the closed-mouth sample.
      sequence += '-' + Math.round(unit(this.amplitude) * 4);
    }
    if (!quiet && this.time - this.petStart < 2 && this.mood !== 'speaking') {
      sequence = 'happy';
      phase = this.time - this.petStart;
    }
    if (!this.images.has(sequence)) sequence = 'idle';
    const entry = this.manifest.sequences[sequence];
    const frame = Math.floor(phase * this.manifest.fps) % entry.frames;
    this.paint(sequence, frame);
    if (!this.fpsStart) this.fpsStart = now;
    this.frameCount++;
    if (now - this.fpsStart >= 1000) {
      this.fps = Math.round(this.frameCount * 1000 / (now - this.fpsStart));
      this.frameCount = 0;
      this.fpsStart = now;
    }
  };

  private paint(sequence: string, frame: number) {
    const key = sequence + ':' + frame + ':' + this.size;
    if (this.lastPaint === key || !this.manifest) return;
    const image = this.images.get(sequence);
    if (!image) return;
    this.sampleContext.drawImage(image, frame % 16 * 128, Math.floor(frame / 16) * 64, 128, 64, 0, 0, 128, 64);
    const source = new Uint32Array(this.sampleContext.getImageData(0, 0, 128, 64).data.buffer);
    const output = new Uint32Array(this.pixels.data.buffer);
    // Exact original screen-pixel -> cell mapping and original C dim palette.
    for (let y = 0; y < this.size; y++) {
      const ym = this.map[y], row = (ym & 127) * 128;
      if (y > 0 && ym === this.map[y - 1]) {
        output.copyWithin(y * this.size, (y - 1) * this.size, y * this.size);
      } else {
        for (let x = 0; x < this.size; x++) {
          const xm = this.map[x];
          output[y * this.size + x] = source[row + (xm & 127) + ((ym | xm) & 128 ? 64 : 0)];
        }
      }
    }
    this.context.putImageData(this.pixels, 0, 0);
    this.lastPaint = key;
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    this.controller.abort();
    this.observer.disconnect();
    this.host.removeEventListener('click', this.onClick);
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.reducedMotion.removeEventListener('change', this.onMotionPreference);
    for (const image of this.loadingImages) image.src = '';
    this.loadingImages.clear();
    this.images.clear();
    this.canvas.remove();
    this.sample.width = this.sample.height = 0;
    this.fps = 0;
  }
}
