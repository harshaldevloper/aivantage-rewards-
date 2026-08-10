// Backup research brain — used when Claude isn't available.
//
// NIM has no web access, so this script does the fetching itself and hands the model
// real, current page text. Without that it would invent tools from training data, which
// is exactly the failure mode that gets an account killed.
//
//   node fallback/research.js          (or double-click make-todays-reel.bat)
//
// Produces: reels/NN-KEYWORD.json, out/reel-NN-KEYWORD.mp4, out/NN-KEYWORD-post.txt

const fs = require('fs');
const path = require('path');
const {
  ROOT, loadEnv, askNim, nextReelName, writeConfig, writePost, renderReel,
} = require('./nim');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/130.0 Safari/537.36';

const strip = (html) => html
  .replace(/<script[\s\S]*?<\/script>/gi, ' ')
  .replace(/<style[\s\S]*?<\/style>/gi, ' ')
  .replace(/<[^>]+>/g, '\n')
  .replace(/&[a-z]+;/gi, ' ')
  .split('\n').map(s => s.trim()).filter(Boolean).join('\n');

async function grab(url, limit = 3500) {
  try {
    const r = await fetch(url, { headers: { 'user-agent': UA }, redirect: 'follow' });
    if (!r.ok) return `[${url} returned ${r.status}]`;
    return strip(await r.text()).slice(0, limit);
  } catch (e) {
    return `[${url} failed: ${e.message}]`;
  }
}

// --- the model call -------------------------------------------------------
async function askForReel(env, sources, covered) {
  const system = `You research free AI tools for an Instagram account and write reel scripts.

HARD RULES:
- The tool's NAME must never appear in the script, caption, or on-screen text. Viewers comment
  a keyword to receive the name by DM. Naming it kills the entire mechanic.
- The comment keyword is a BENEFIT word (CLIPS, NOTES, EDIT), never the tool name, 4-8 letters.
- Spell out initialisms so text-to-speech reads them properly: "A.I.", "D.M.", "P.D.F.", "H.D.".
- Never claim a free tier you cannot see evidence for in the supplied source text. If limits are
  unclear, describe it as free without inventing numbers.
- Never pick a voice-cloning tool that requires no account. Synthetic-voice TTS is fine.
- The script must be 18-26 seconds spoken (roughly 55-80 words), end with the comment keyword
  and then a follow-gate.
- Include exactly three short parallel phrases in the middle, all starting with "No "
  (e.g. "No download. No account. No watermark.") — they must appear verbatim in the script.

Reply with ONLY a JSON object, no markdown fence, no commentary:
{
  "tool": "actual tool name",
  "url": "homepage url",
  "why_free": "what is actually free, from the sources",
  "keyword": "EDIT",
  "palette": "cinema|ember|mint|violet",
  "script": "the full spoken script",
  "stackPhrases": ["No download","No account","No watermark"],
  "highlight": ["free","edit"],
  "caption": "keyword-rich first line naming the CATEGORY not the tool, then the comment instruction, then the follow-gate, then 4-6 hashtags",
  "altText": "one plain sentence describing the tool category",
  "dm": "the auto-DM: the link delivered immediately, then one soft second line inviting them to reply MORE",
  "affiliate": "signup URL for that tool's affiliate program, or the word none"
}`;

  const user = `Today is ${new Date().toISOString().slice(0, 10)}.

ALREADY COVERED — do not pick any of these, and do not reuse these keywords:
${covered}

CURRENT SOURCE PAGES:
${sources}

Pick ONE tool that is genuinely free and useful to a normal person in a browser, and that is
not in the covered list. Prefer something aimed at one specific profession or hobby. Write the
reel for it.`;

  return askNim(env, { system, user, maxTokens: 1600 });
}

// --- main -----------------------------------------------------------------
(async () => {
  const env = loadEnv();

  console.log('fetching sources...');
  const sources = (await Promise.all([
    grab('https://github.com/trending?since=daily'),
    grab('https://huggingface.co/spaces'),
    grab('https://launchaijam.com/new-ai-tools'),
  ])).join('\n\n---\n\n');

  const coveredPath = path.resolve(ROOT, '..', 'covered.md');
  const covered = fs.existsSync(coveredPath)
    ? fs.readFileSync(coveredPath, 'utf8') : '(none yet)';

  console.log('asking NVIDIA NIM...');
  const pick = await askForReel(env, sources, covered);
  console.log(`  -> ${pick.tool}  [${pick.keyword}]`);

  const name = nextReelName(pick.keyword);

  const cfg = {
    name, scene: 'scene-mascot.html', keyword: pick.keyword,
    palette: pick.palette || 'cinema',
    voice: 'en-US-AndrewMultilingualNeural', rate: '+8%',
    script: pick.script,
    highlight: pick.highlight || [],
    hot: ['no', 'not', 'naming', 'follow', 'first'],
    stackPhrases: pick.stackPhrases || [],
    sub: "FOLLOW FIRST OR THE DM WON'T SEND",
    beats: [
      { cue: 0, pose: 'present', expr: 'wide'    },
      { cue: 1, pose: 'shrug',   expr: 'sly'     },
      { cue: 4, pose: 'point',   expr: 'neutral' },
      { cue: 6, pose: 'cheer',   expr: 'happy'   },
      { cue: 7, pose: 'hush',    expr: 'sly'     },
      { cue: 8, pose: 'point',   expr: 'happy'   },
    ],
  };
  const cfgPath = writeConfig(cfg);

  writePost(name,
`TOOL      ${pick.tool}
LINK      ${pick.url}
WHAT'S FREE
${pick.why_free}

=========================== CAPTION ===========================
${pick.caption}

=========================== ALT TEXT ==========================
${pick.altText}

===================== AUTO-DM FOR "${pick.keyword}" =====================
${pick.dm}

========================== AFFILIATE ==========================
${pick.affiliate}

========================== WHEN TO POST =======================
8-9 PM IST. Post Sun-Thu only, skip Fri and Sat. One reel per day, no more.

========================== BEFORE POSTING =====================
Open ${pick.url} yourself and confirm the free tier is what this says.
This was written by a backup model with no browsing - it is more likely
to be wrong than the usual process. Verify, then post.
`);

  if (process.argv.includes('--dry')) {
    console.log(`\nDRY RUN — skipped rendering.`);
    console.log(`  config: reels/${name}.json`);
    console.log(`  post copy: out/${name}-post.txt`);
    return;
  }

  console.log('rendering...');
  renderReel(cfgPath);

  console.log(`\nDONE`);
  console.log(`  video: out/reel-${name}.mp4`);
  console.log(`  caption + alt text + DM + affiliate: out/${name}-post.txt`);
})().catch(e => { console.error('\nFAILED: ' + e.message); process.exit(1); });
