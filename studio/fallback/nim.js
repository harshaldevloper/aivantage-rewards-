// Shared plumbing for the fallback reel writers (research.js, from-research.js).
// Both need the same four things: the .env key, a NIM call that survives retired
// models, a next-in-sequence reel name, and somewhere to put the config/copy.

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const NIM_URL = 'https://integrate.api.nvidia.com/v1/chat/completions';
const DEFAULT_MODEL = 'meta/llama-3.1-70b-instruct';

function loadEnv() {
  const p = path.join(ROOT, '.env');
  if (!fs.existsSync(p)) throw new Error('.env not found at ' + p);
  const env = {};
  for (const line of fs.readFileSync(p, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
    if (m) env[m[1]] = m[2].trim();
  }
  if (!env.NVIDIA_API_KEY) throw new Error('NVIDIA_API_KEY missing from .env');
  return env;
}

// Models get retired without notice, so walk the list until one answers with
// parseable JSON, and report every failure if none does.
async function askNim(env, { system, user, maxTokens = 1600, temperature = 0.7 }) {
  const models = (env.NIM_MODELS || DEFAULT_MODEL)
    .split(',').map(s => s.trim()).filter(Boolean);
  const problems = [];

  for (const model of models) {
    try {
      const res = await fetch(NIM_URL, {
        method: 'POST',
        headers: { authorization: `Bearer ${env.NVIDIA_API_KEY}`,
                   'content-type': 'application/json' },
        body: JSON.stringify({
          model, temperature, max_tokens: maxTokens,
          messages: [{ role: 'system', content: system },
                     { role: 'user', content: user }],
        }),
        signal: AbortSignal.timeout(120000),
      });
      if (!res.ok) { problems.push(`${model}: HTTP ${res.status}`); continue; }
      const txt = (await res.json()).choices[0].message.content;
      const m = txt.match(/\{[\s\S]*\}/);
      if (!m) { problems.push(`${model}: no JSON in reply`); continue; }
      console.log(`  (model: ${model})`);
      return JSON.parse(m[0]);
    } catch (e) {
      problems.push(`${model}: ${e.message}`);
    }
  }
  throw new Error('Every model failed:\n  ' + problems.join('\n  ') +
    '\nPick working ones from https://build.nvidia.com and update NIM_MODELS in .env');
}

// Configs are numbered by how many already exist, zero-padded so reels/ sorts.
function nextReelName(keyword, prefix = '') {
  const n = String(fs.readdirSync(path.join(ROOT, 'reels'))
    .filter(f => f.endsWith('.json')).length + 1).padStart(2, '0');
  return `${prefix}${n}-${String(keyword).toLowerCase()}`;
}

function writeConfig(cfg) {
  const cfgPath = path.join(ROOT, 'reels', `${cfg.name}.json`);
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  return cfgPath;
}

// Everything that would otherwise be handed over in chat, written to a file.
function writePost(name, body) {
  const out = path.join(ROOT, 'out');
  fs.mkdirSync(out, { recursive: true });
  const txt = path.join(out, `${name}-post.txt`);
  fs.writeFileSync(txt, body, 'utf8');
  return txt;
}

function renderReel(cfgPath) {
  execFileSync('node', [path.join(ROOT, 'make-reel.js'), cfgPath],
               { stdio: 'inherit', cwd: ROOT });
}

module.exports = {
  ROOT, loadEnv, askNim, nextReelName, writeConfig, writePost, renderReel,
};
