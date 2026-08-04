// Emits the copy pack for reels that were rendered before the post.txt step existed.
//   node write-captions.js
// Instagram is posted manually, so each reel needs its caption, alt text and DM ready to go.

const fs = require('fs');
const path = require('path');
const OUT = path.join(__dirname, 'out');

const REELS = [
  {
    name: 'b01-notes', keyword: 'NOTES', tool: 'Gemini Notebook',
    url: 'https://notebooklm.google.com',
    category: 'free AI study tool that turns your notes into a podcast',
    free: '100 notebooks, 50 sources each, and 3 audio overviews per day. Resets daily. No card needed.',
    catch: 'The 3-per-day audio limit is the real cap. Everything else is generous.',
    tags: '#studytok #studenthacks #aiforstudents #freeaitools #studytips',
    date: 'Sun Aug 9',
  },
  {
    name: 'b02-teach', keyword: 'TEACH', tool: 'MagicSchool AI',
    url: 'https://www.magicschool.ai',
    category: 'free AI lesson planner for teachers',
    free: '80+ teacher tools and 50+ student tools on the free plan. Available worldwide.',
    catch: 'A monthly generation cap exists but is NOT published. Say "limited free generations", never a number. No output history or editing on free.',
    tags: '#teachersofinstagram #teacherhacks #edtech #freeaitools #teachertips',
    date: 'Mon Aug 10',
  },
  {
    name: 'b03-offline', keyword: 'OFFLINE', tool: 'Ollama',
    url: 'https://ollama.com',
    category: 'free open source AI that runs offline on your own computer',
    free: 'Free forever, open source. No account, no internet, no cost per message.',
    catch: 'Needs a decent machine and a terminal. Say so yourself before the comments do.',
    tags: '#opensource #privacy #localai #freeaitools #aitools',
    date: 'Tue Aug 11',
  },
  {
    name: 'b04-voice', keyword: 'VOICE', tool: 'ElevenLabs',
    url: 'https://elevenlabs.io',
    category: 'free AI voice generator for creators',
    free: '10,000 characters per month, 70+ languages, no credit card.',
    catch: 'Voice cloning is paid only. 10k characters is roughly 12 minutes of speech.',
    tags: '#aivoice #contentcreatortips #voiceover #freeaitools #aiforcreators',
    date: 'Wed Aug 12',
    affiliate: 'https://elevenlabs.io/affiliates - NOT registered yet',
  },
  {
    name: 'b05-resume', keyword: 'RESUME', tool: 'FreeCV',
    url: 'https://freecv.org/ai-resume-builder',
    category: 'free AI resume builder with no signup',
    free: 'No signup, no payment, no watermark. Rewrites bullets, writes your summary, scores you against the job description, exports PDF.',
    catch: 'Their own FAQ says "generous AI usage", not unlimited. Do not promise unlimited.',
    tags: '#jobsearch #resumetips #careeradvice #freeaitools #jobhunting',
    date: 'Thu Aug 13',
  },
  {
    name: 'b06-prompt', keyword: 'PROMPT', tool: 'InVideo AI',
    url: 'https://invideo.io',
    category: 'AI that turns one sentence into a finished edited video',
    free: 'A free tier exists but it watermarks the output. Confirm current limits before posting.',
    catch: 'Watermark on free. Be upfront about it - that honesty is what makes the rest believable.',
    tags: '#aivideo #contentcreatortips #videoediting #aiforcreators #freeaitools',
    date: 'Sun Aug 16',
    affiliate: 'https://invideo.io/affiliate/ - 50% of first payment. NOT registered yet.',
  },
];

fs.mkdirSync(OUT, { recursive: true });
const cap = s => s.charAt(0).toUpperCase() + s.slice(1);

for (const r of REELS) {
  const body =
`FILE      reel-${r.name}.mp4
POST ON   ${r.date}, 8-9 PM IST
TOOL      ${r.tool}   <-- never say this on screen or in the caption
LINK      ${r.url}

========================= INSTAGRAM CAPTION =========================
${cap(r.category)} - and I'm not naming it in the video
Comment ${r.keyword} and I'll send you the link.
Follow first, or the DM can't reach you.

${r.tags}

============================== ALT TEXT =============================
${cap(r.category)}

===================== AUTO-DM FOR "${r.keyword}" =====================
Here you go: ${r.url}

${r.free}

Btw I test around 20 of these a week and only 2-3 make the feed.
Reply MORE and I'll send you the ones that don't.

============================== THE CATCH ============================
${r.catch}

=================== YOUTUBE SHORT (same MP4) ========================
Vertical and under 3 minutes, so it uploads as a Short unchanged.
YouTube NAMES the tool - the opposite of Instagram, and correct.

Title:
${r.tool}: ${cap(r.category)} #Shorts

Description:
${r.free}

${r.catch}

Try it: ${r.url}
More free AI tools tested weekly: https://instagram.com/aivantage_ai

============================= AFFILIATE =============================
${r.affiliate || 'None - this one is a pure reach play.'}

============================= REMINDERS =============================
- Set the comment-to-DM trigger BEFORE publishing, never after.
- Reply to every comment in the first hour. Heaviest ranking signal there is.
- On YouTube, toggle "Altered or synthetic content" - synthetic voice.
`;
  fs.writeFileSync(path.join(OUT, `${r.name}-post.txt`), body, 'utf8');
  console.log(`wrote out/${r.name}-post.txt   [${r.keyword}]  ${r.date}`);
}
console.log(`\n${REELS.length} copy packs written.`);
