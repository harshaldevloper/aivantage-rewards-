// Turns research YOU did into a finished reel + all its copy.
//
//   node fallback/from-research.js      (or double-click make-todays-reel.bat)
//
// The old version had a small model guess at tools from scraped pages. This one asks you to
// paste what you actually verified, so the model only WRITES — it never decides what is true.
// That removes the one failure mode that can kill the account: promising a free tier that
// does not exist.

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');

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

function askBlock(prompt) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    console.log(prompt);
    const lines = [];
    let blanks = 0;
    rl.on('line', (l) => {
      if (l.trim() === '') {
        if (++blanks >= 1 && lines.length) { rl.close(); return; }
      } else { blanks = 0; lines.push(l); }
    });
    rl.on('close', () => resolve(lines.join('\n').trim()));
  });
}

const SYSTEM = `You write Instagram reel scripts and SEO copy for an AI-tools account.

You are given research the user VERIFIED THEMSELVES. Treat it as the only source of truth.
Never add a limit, number, or feature that is not in their research. If something is not
stated, do not mention it.

HARD RULES:
- The tool's NAME must NEVER appear in the script, the caption, or any on-screen text. Viewers
  comment a keyword to receive the name by DM. Naming it destroys the entire mechanic.
- The comment keyword is a BENEFIT word (CLIPS, NOTES, EDIT, RESUME), never the tool name,
  4-8 letters, easy to type on a phone.
- Spell out initialisms so text-to-speech says them properly: "A.I.", "D.M.", "P.D.F.", "C.V.",
  "H.D.". Without the dots the voice reads them as words.
- Script must be 40-70 words. End with the comment keyword, then a follow-gate.
- Include exactly three short parallel phrases in the middle, all starting with "No "
  (e.g. "No signup. No payment. No watermark.") and they must appear VERBATIM in the script.
- The caption's FIRST LINE must contain the tool's CATEGORY as a search phrase
  (e.g. "free AI resume builder") but never the tool's name.
- 4-6 hashtags. Never 30.

Reply with ONLY a JSON object, no markdown fence, no commentary:
{
  "keyword": "RESUME",
  "palette": "cinema|ember|mint|violet",
  "script": "the full spoken script",
  "stackPhrases": ["No signup","No payment","No watermark"],
  "highlight": ["free","resume"],
  "demoTitle": "short window title, 2-3 words, NOT the tool name",
  "demoAct": "process|edit|slides|video",
  "demoInput": "a filename or prompt shown typing in the panel",
  "demoAction": "short progress label e.g. Rewriting bullets",
  "demoOutputLabel": "one word for result tiles e.g. Draft",
  "caption": "first line with the category as a search phrase, then the comment instruction, then the follow-gate, then hashtags",
  "altText": "one plain sentence describing the tool category",
  "dm": "the auto-DM: the link delivered immediately, then one soft second line inviting them to reply MORE",
  "ytTitle": "a YouTube title that NAMES the tool - youtube is search, names are required there",
  "ytDescription": "2 keyword-rich opening lines, then what is free, then the catch"
}`;

async function askNim(env, research) {
  const models = (env.NIM_MODELS || 'meta/llama-3.1-70b-instruct')
    .split(',').map(s => s.trim()).filter(Boolean);
  const problems = [];
  for (const model of models) {
    try {
      const res = await fetch('https://integrate.api.nvidia.com/v1/chat/completions', {
        method: 'POST',
        headers: { authorization: `Bearer ${env.NVIDIA_API_KEY}`, 'content-type': 'application/json' },
        body: JSON.stringify({
          model, temperature: 0.7, max_tokens: 1800,
          messages: [{ role: 'system', content: SYSTEM },
                     { role: 'user', content: `MY VERIFIED RESEARCH:\n\n${research}` }],
        }),
        signal: AbortSignal.timeout(120000),
      });
      if (!res.ok) { problems.push(`${model}: HTTP ${res.status}`); continue; }
      const body = await res.json();
      const txt = body?.choices?.[0]?.message?.content;
      if (typeof txt !== 'string') {
        problems.push(`${model}: unexpected response shape ${JSON.stringify(body).slice(0, 160)}`);
        continue;
      }
      const m = txt.match(/\{[\s\S]*\}/);
      if (!m) { problems.push(`${model}: no JSON`); continue; }
      console.log(`  (model: ${model})`);
      return JSON.parse(m[0]);
    } catch (e) { problems.push(`${model}: ${e.message}`); }
  }
  throw new Error('Every model failed:\n  ' + problems.join('\n  '));
}

(async () => {
  const env = loadEnv();

  console.log('\n=================================================================');
  console.log(' Paste the research you verified. Include:');
  console.log('   - tool name and its URL');
  console.log('   - EXACTLY what is free (the real limits, from its pricing page)');
  console.log('   - the catch (watermark? ads? credits? signup?)');
  console.log('   - who it is for (students, teachers, job seekers...)');
  console.log('');
  console.log(' Press ENTER on a blank line when you are done.');
  console.log('=================================================================\n');

  const research = await askBlock('> ');
  if (!research || research.length < 40) {
    console.error('\nThat is too short to work from. Run it again with the real details.');
    process.exit(1);
  }

  console.log('\nwriting the reel...');
  const r = await askNim(env, research);
  for (const field of ['keyword', 'script', 'caption']) {
    if (typeof r[field] !== 'string' || !r[field].trim()) {
      throw new Error(`the model's reply has no usable "${field}". Re-run, or try a\n` +
        '  different model in NIM_MODELS.');
    }
  }
  console.log(`  keyword: ${r.keyword}`);

  const n = String(fs.readdirSync(path.join(ROOT, 'reels'))
    .filter(f => f.endsWith('.json')).length + 1).padStart(2, '0');
  const name = `r${n}-${String(r.keyword).toLowerCase()}`;

  const cfg = {
    name, scene: 'scene-mascot.html', keyword: r.keyword,
    palette: r.palette || 'cinema',
    voice: 'en-US-AndrewMultilingualNeural', rate: '+8%',
    script: r.script,
    highlight: r.highlight || [],
    hot: ['no', 'not', 'nothing', 'only', 'followers'],
    stackPhrases: r.stackPhrases || [],
    sub: "IT ONLY DMS PEOPLE WHO FOLLOW",
    demo: {
      act: r.demoAct || 'process',
      title: r.demoTitle || 'browser tab',
      inputIcon: '+', input: r.demoInput || '',
      action: r.demoAction || 'Working',
      outputs: 3, cols: 3, outputLabel: r.demoOutputLabel || 'Result',
      tileHeight: 140, start: 2.6, end: 11,
    },
    beats: [
      { cue: 0, pose: 'present', expr: 'wide' },
      { cue: 1, pose: 'point', expr: 'neutral' },
      { cue: 4, pose: 'shrug', expr: 'sly' },
      { cue: 5, pose: 'cheer', expr: 'happy' },
      { cue: 6, pose: 'point', expr: 'happy' },
    ],
  };
  const cfgPath = path.join(ROOT, 'reels', `${name}.json`);
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

  fs.mkdirSync(path.join(ROOT, 'out'), { recursive: true });
  const txt = path.join(ROOT, 'out', `${name}-post.txt`);
  fs.writeFileSync(txt,
`========================= INSTAGRAM CAPTION =========================
${r.caption}

============================== ALT TEXT =============================
${r.altText}

======================= AUTO-DM FOR "${r.keyword}" =======================
${r.dm}

=================== YOUTUBE (short - same MP4) ======================
Title:
${r.ytTitle}

Description:
${r.ytDescription}

Shorts are up to 3 minutes and vertical, so this reel qualifies as-is.
Upload the same file. YouTube names the tool - Instagram does not.

============================ REMINDERS ==============================
- Set the comment-to-DM trigger BEFORE publishing, not after.
- 8-9 PM IST, Sunday to Thursday. Never Friday or Saturday.
- One reel per day. No exceptions.
- On YouTube, toggle "Altered or synthetic content" - synthetic voice.

========================= YOUR RESEARCH =============================
${research}
`, 'utf8');

  console.log('rendering...\n');
  execFileSync('node', [path.join(ROOT, 'make-reel.js'), cfgPath],
               { stdio: 'inherit', cwd: ROOT });

  console.log('\n=================================================================');
  console.log(` VIDEO    ${path.join(ROOT, 'out', `reel-${name}.mp4`)}`);
  console.log(` COPY     ${txt}`);
  console.log('=================================================================');
})().catch(e => { console.error('\nFAILED: ' + e.message); process.exit(1); });
