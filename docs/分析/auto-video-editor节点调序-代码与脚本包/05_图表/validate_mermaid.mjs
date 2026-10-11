// 用 mermaid 官方解析器校验 .mmd / .md 中的 Mermaid 源码语法。
// 用法: node validate_mermaid.mjs <目录或文件> [...]
//   - .mmd 文件: 整个文件作为一张图
//   - .md  文件: 提取所有 ```mermaid 代码块逐个校验
// 依赖: npm i mermaid@11 jsdom@24
import { JSDOM } from 'jsdom';
import fs from 'node:fs';
import path from 'node:path';

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const mermaid = (await import('mermaid')).default;
mermaid.initialize({ startOnLoad: false });

const targets = process.argv.slice(2);
const files = [];
for (const t of targets) {
  const st = fs.statSync(t);
  if (st.isDirectory()) for (const f of fs.readdirSync(t)) files.push(path.join(t, f));
  else files.push(t);
}
let bad = 0, total = 0;
for (const f of files) {
  const ext = path.extname(f).toLowerCase();
  if (ext !== '.mmd' && ext !== '.md') continue;
  const txt = fs.readFileSync(f, 'utf8');
  const blocks = ext === '.mmd' ? [txt] : [...txt.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map(m => m[1]);
  let i = 0;
  for (const b of blocks) {
    i++; total++;
    try { await mermaid.parse(b); console.log(`OK    ${path.basename(f)} #${i}  (${b.split('\n').length} 行)`); }
    catch (e) { bad++; console.log(`FAIL  ${path.basename(f)} #${i}: ${String(e.message || e).split('\n').slice(0, 4).join(' | ')}`); }
  }
}
console.log(`\n共 ${total} 张,失败 ${bad} 张`);
process.exit(bad ? 1 : 0);
