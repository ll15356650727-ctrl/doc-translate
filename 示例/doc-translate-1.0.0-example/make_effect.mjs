import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const sharp = require('/Users/clown/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/sharp');
const root = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(root, 'input/help.md'), 'utf8').split('\n');
const target = fs.readFileSync(path.join(root, 'work/translated/help.md'), 'utf8').split('\n');
const summary = JSON.parse(fs.readFileSync(path.join(root, 'work/summary.json'), 'utf8'));
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
const selectedSource = [source.find(s=>s.startsWith('团队负责人')), source.find(s=>s.startsWith('2.')), source.find(s=>s.startsWith('| 截止日期')), source.find(s=>s.startsWith('| 提醒阈值'))];
const selectedTarget = [target.find(s=>s.startsWith('A team lead')), target.find(s=>s.startsWith('2.')), target.find(s=>s.startsWith('| Deadline')), target.find(s=>s.startsWith('| Reminder threshold'))];
if ([...selectedSource,...selectedTarget].some(s=>!s)) throw new Error('实际示例片段缺失');
function wrap(text, budget=23) {
  const lines=[]; let line='',size=0;
  for (const word of text.match(/[^\x00-\xff]|[A-Za-z0-9]+\s*|./gu)||[]) {
    const width=[...word].reduce((a,c)=>a+(/[^\x00-\xff]/.test(c)?1:0.55),0);
    if (size+width>budget && line) {lines.push(line);line='';size=0;}
    line+=word;size+=width;
  }
  if(line)lines.push(line);
  return lines;
}
function panel(x,title,items) {
  let y=300;
  let out=`<rect x="${x}" y="190" width="640" height="690" rx="22" fill="white" stroke="#C5D8D2"/><text x="${x+30}" y="245" font-size="28" font-weight="700" fill="#173F37">${title}</text>`;
  for (const item of items) {
    for (const line of wrap(item)) {out+=`<text x="${x+30}" y="${y}" font-size="24" fill="#223D36">${esc(line)}</text>`;y+=38;}
    y+=28;
  }
  return out;
}
const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="1080" viewBox="0 0 1500 1080"><rect width="1500" height="1080" fill="#EFF5F2"/><g font-family="PingFang SC,Arial,sans-serif"><text x="60" y="80" font-size="40" font-weight="700" fill="#153B32">专业文档翻译与校对</text><text x="60" y="132" font-size="25" fill="#526D65">1.0.0 · 实际运行示例 · 同一份 Markdown 的片段摘录</text>${panel(50,'输入 · 原始文档',selectedSource)}${panel(810,'输出 · 实际生成的译文',selectedTarget)}<path d="M718 520h61m-18-18 18 18-18 18" fill="none" stroke="#17785E" stroke-width="7" stroke-linecap="round"/><text x="60" y="945" font-size="26" fill="#173F37">再次处理相同输入：${summary.reused_segments}/${summary.segments} 个片段复用，新增待译 ${summary.pending_unique} 个</text><text x="60" y="996" font-size="23" fill="#526D65">数值、日期、代码、链接和变量受保护；保留原文件。复用统计不等于 Token 实测。</text><text x="60" y="1042" font-size="20" fill="#526D65">来源：input/help.md → work/translated/help.md；完整文件与校验报告随示例保存。</text></g></svg>`;
fs.writeFileSync(path.join(root,'effect.svg'),svg);
await sharp(Buffer.from(svg)).png().toFile(path.join(root,'effect.png'));
console.log(path.join(root,'effect.png'));
