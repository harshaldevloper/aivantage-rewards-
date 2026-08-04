// Turns a reel config (reels/*.json) into a finished, upload-ready MP4.
//   node make-reel.js reels/01-clips.json
// Pipeline: edge-tts voiceover -> phrase timings -> headless-Chromium frames -> ffmpeg mux.
const puppeteer = require('puppeteer');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const FPS = 30;
const PORTRAIT = { width: 1080, height: 1920 };   // reels/shorts
const LANDSCAPE = { width: 1920, height: 1080 };  // youtube long-form
const ROOT = __dirname;
// Windows venvs put the interpreter in Scripts\, POSIX ones in bin/. CI has no
// venv at all and installs edge-tts against the system Python, so fall back to
// whatever is on PATH rather than a path that only exists on one OS.
const PY = process.env.PYTHON_BIN || (() => {
  const candidates = [
    path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(ROOT, '.venv', 'bin', 'python'),
  ];
  return candidates.find(p => fs.existsSync(p))
    || (process.platform === 'win32' ? 'python' : 'python3');
})();
const OUT = path.join(ROOT, 'out');
// Per-render frame directory. One shared folder raced on Windows — parallel browsers do not
// always release handles before the next run clears it (ENOTEMPTY), and two renders at once
// would overwrite each other's frames.
const FRAMES_ROOT = path.join(ROOT, 'frames');

const cfgPath = process.argv[2];
if (!cfgPath) { console.error('usage: node make-reel.js reels/<name>.json'); process.exit(1); }
const cfg = JSON.parse(fs.readFileSync(path.resolve(cfgPath), 'utf8'));
const name = cfg.name || path.basename(cfgPath, '.json');

const norm = s => s.toLowerCase().replace(/[^a-z0-9 ]/g, '').trim();

function parseVtt(txt) {
  // edge-tts emits SRT-style blocks: index / hh:mm:ss,mmm --> hh:mm:ss,mmm / text
  const secs = ts => {
    const [h, m, rest] = ts.trim().split(':');
    return (+h) * 3600 + (+m) * 60 + parseFloat(rest.replace(',', '.'));
  };
  const cues = [];
  for (const block of txt.trim().split(/\r?\n\r?\n/)) {
    const lines = block.split(/\r?\n/).filter(Boolean);
    const tl = lines.find(l => l.includes('-->'));
    if (!tl) continue;
    const [a, b] = tl.split('-->');
    const text = lines.slice(lines.indexOf(tl) + 1).join(' ').trim();
    if (text) cues.push([secs(a), secs(b), text]);
  }
  return cues;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  // 1. voiceover ------------------------------------------------------------
  const scriptFile = path.join(OUT, `${name}.txt`);
  fs.writeFileSync(scriptFile, cfg.script, 'utf8');
  const mp3 = path.join(OUT, `${name}.mp3`);
  const vtt = path.join(OUT, `${name}.vtt`);
  console.log('generating voiceover...');
  execFileSync(PY, ['-m', 'edge_tts',
    '--voice', cfg.voice || 'en-US-AndrewMultilingualNeural',
    `--rate=${cfg.rate || '+8%'}`,
    '--file', scriptFile, '--write-media', mp3, '--write-subtitles', vtt,
  ], { stdio: 'inherit' });

  const cues = parseVtt(fs.readFileSync(vtt, 'utf8'));
  const duration = parseFloat(execFileSync('ffprobe', ['-v', 'error',
    '-show_entries', 'format=duration', '-of',
    'default=noprint_wrappers=1:nokey=1', mp3]).toString().trim());
  console.log(`  ${cues.length} phrases, ${duration.toFixed(2)}s`);

  // 2. resolve which cues stack, and where the CTA fires ---------------------
  const stackSet = new Set((cfg.stackPhrases || []).map(norm));
  const stackCues = cues.map((c, i) => stackSet.has(norm(c[2])) ? i : -1).filter(i => i >= 0);
  // The CTA is the "comment the word X" line. Search from the END — the keyword often
  // appears in the opening hook too ("...six video CLIPS"), and matching that would park
  // the pill on screen for the whole reel.
  let ctaCue = cfg.ctaCue;
  if (ctaCue == null) {
    ctaCue = cues.map(c => norm(c[2])).findLastIndex(s => s.includes('comment'));
    if (ctaCue < 0) {
      const kw = norm(cfg.keyword);
      ctaCue = cues.map(c => norm(c[2]).split(' ')).findLastIndex(ws => ws.includes(kw));
    }
    if (ctaCue < 0) ctaCue = Math.max(0, cues.length - 2);
  }
  console.log(`  stack cues [${stackCues}], cta at cue ${ctaCue}`);

  const sceneCfg = {
    cues, duration, keyword: cfg.keyword, sub: cfg.sub,
    palette: cfg.palette || 'cinema',
    highlight: cfg.highlight || [], hot: cfg.hot || [],
    stackCues, ctaCue, beats: cfg.beats || [],
    demo: cfg.demo || null,
    segments: cfg.segments || [],
    reveal: cfg.reveal || null,   // set on the YouTube cut — names the tool instead of a CTA
  };

  const { width: W, height: H } =
    cfg.format === 'landscape' ? LANDSCAPE : (cfg.format || PORTRAIT);

  // 3. render frames --------------------------------------------------------
  // Frames are independent — seek(t) is deterministic — so they parallelise cleanly.
  // Each worker owns a contiguous slice and its own browser. Roughly linear speedup.
  const FRAMES = path.join(FRAMES_ROOT, name);
  fs.rmSync(FRAMES, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  fs.mkdirSync(FRAMES, { recursive: true });

  const sceneFile = cfg.scene || 'scene.html';
  const sceneUrl = 'file:///' + path.join(ROOT, sceneFile).replace(/\\/g, '/');
  const total = Math.ceil(duration * FPS);
  // Each worker is a full Chromium (~250-350MB). This machine has 7.5GB and the user's own
  // browser is usually open, so budget by FREE memory, not core count — over-subscribing
  // does not error, it hangs, which is far harder to diagnose.
  const freeMb = os.freemem() / 1048576;
  const byMemory = Math.max(1, Math.floor(freeMb / 400));
  const workers = Math.max(1, Math.min(cfg.workers || 3, byMemory, os.cpus().length - 1, total));
  if (workers < (cfg.workers || 3)) {
    console.log(`  (${Math.round(freeMb)}MB free — dropping to ${workers} worker(s))`);
  }
  const t0 = Date.now();
  let done = 0;

  console.log(`rendering ${total} frames at ${W}x${H} across ${workers} workers...`);

  const slice = Math.ceil(total / workers);
  await Promise.all(Array.from({ length: workers }, async (_, w) => {
    const from = w * slice, to = Math.min(total, from + slice);
    if (from >= to) return;
    const browser = await puppeteer.launch({
      headless: 'new', protocolTimeout: 180000,
      args: ['--no-sandbox', '--force-device-scale-factor=1', '--hide-scrollbars',
             '--font-render-hinting=none', '--disable-lcd-text'],
    });
    try {
      const page = await browser.newPage();
      await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
      await page.goto(sceneUrl, { waitUntil: 'networkidle0' });
      await page.evaluate(c => window.init(c), sceneCfg);
      for (let i = from; i < to; i++) {
        await page.evaluate(tt => window.seek(tt), i / FPS);
        await page.screenshot({
          path: path.join(FRAMES, `f${String(i).padStart(5, '0')}.jpg`),
          type: 'jpeg', quality: 94, optimizeForSpeed: true,
        });
        if (++done % 150 === 0 || done === total) {
          console.log(`  frames ${((done / total) * 100).toFixed(0)}%  ` +
                      `${((Date.now() - t0) / 1000).toFixed(0)}s`);
        }
      }
    } finally { await browser.close(); }
  }));

  // 4. mux ------------------------------------------------------------------
  const mp4 = path.join(OUT, `reel-${name}.mp4`);
  console.log('encoding...');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error',
    '-framerate', String(FPS), '-i', path.join(FRAMES, 'f%05d.jpg'), '-i', mp3,
    // 'slow' buffers many frames and ran out of memory on this 7.5GB machine (x264 malloc
    // failure). 'medium' at the same CRF is visually indistinguishable here and far leaner.
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '19', '-threads', '2',
    '-profile:v', 'high', '-level', '4.1', '-pix_fmt', 'yuv420p',
    '-c:a', 'aac', '-b:a', '192k', '-ar', '44100',
    '-shortest', '-movflags', '+faststart', mp4], { stdio: 'inherit' });

  fs.rmSync(FRAMES, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  const mb = (fs.statSync(mp4).size / 1048576).toFixed(2);
  console.log(`\nDONE  ${mp4}  (${mb} MB, ${(duration/60).toFixed(1)} min, ${W}x${H})`);
})().catch(e => { console.error(e); process.exit(1); });
