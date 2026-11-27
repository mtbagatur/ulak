'use strict';
// Transcript text -> HTML. Everything is escaped first and every tag is written here; markup in the text stays text.
// Every pattern is bounded or linear: this runs on whatever an agent printed.
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Sentence punctuation after a path or address is not part of it. Counted from the end, so a long run costs one pass.
function splitTrailing(found) {
  let end = found.length;
  while (end && '),.!?;:'.includes(found[end - 1])) end--;
  return [found.slice(0, end), found.slice(end)];
}

// One run of text. `link(label, href)` returns an anchor, or null when the target may not be linked.
function inlineMd(value, link) {
  const held = [];
  const restore = html => html.replace(/\u0001(\d+)\u0002/g, (_, n) => held[Number(n)] || '');
  const hold = html => `\u0001${held.push(restore(html)) - 1}\u0002`;
  let source = String(value ?? '').replace(/[\u0001\u0002]/g, '');
  source = source.replace(/```[^\n]{0,200}\n([\s\S]*?)```/g, (_, code) => hold(`<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`));
  source = source.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${esc(code)}</code>`));
  // A picture is linked like any other file; the "!" goes, and a picture without a name is called by its path.
  source = source.replace(/(!?)\[([^\]\n]{0,300})\]\(([^)\s\u0001\u0002]{1,500})\)/g, (whole, bang, label, href) => {
    if (!label && !bang) return whole;
    const html = link(label || href, href);
    return html ? hold(html) : whole;
  });
  // A path starts where a word starts, so docs/notes.md links whole (relative to the session's folder), not from its first slash.
  source = source.replace(/(^|[^\w.\-\/])((?:\/|\.\.?\/|[\w.\-]+\/)[^\s<>"'`\u0001\u0002]+)/g, (raw, before, found) => {
    const [path, trailing] = splitTrailing(found);
    const html = link(path, path);
    return html ? before + hold(html) + trailing : raw;
  });
  let html = esc(source).replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  html = html.replace(/https?:\/\/[^\s<>"'\u0001\u0002]+/gi, raw => {
    const [url, trailing] = splitTrailing(raw);
    return (link(url, url) || esc(url)) + trailing;
  });
  return restore(html);
}

const MD_FENCE = /^([ \t]*)```([^`]*)$/;
const MD_HEADING = /^(#{1,4})[ \t]+(\S.*)$/;
const MD_ITEM = /^([ \t]*)([-*]|\d{1,9}\.)[ \t]+(.*)$/;

// A row cut at its pipes. A pipe inside `code` or written as \| belongs to the cell.
function mdCells(line) {
  const text = line.trim(), cells = [];
  let cell = '';
  for (let i = text[0] === '|' ? 1 : 0; i < text.length; i++) {
    const c = text[i];
    if (c === '\\' && text[i + 1] === '|') { cell += '|'; i++; continue; }
    if (c === '`') {
      const close = text.indexOf('`', i + 1);
      if (close > 0) { cell += text.slice(i, close + 1).replaceAll('\\|', '|'); i = close; continue; }
    }
    if (c === '|') { cells.push(cell.trim()); cell = ''; if (i === text.length - 1) return cells; continue; }
    cell += c;
  }
  cells.push(cell.trim());
  return cells;
}
// The line under a table's head: dashes, with colons for alignment (read past, not applied).
const mdRule = line => line !== undefined && line.includes('-') && line.includes('|') && mdCells(line).every(c => /^:?-+:?$/.test(c));
const mdTableAt = (lines, i) => lines[i].includes('|') && mdRule(lines[i + 1]);
const mdStarts = (lines, i) => MD_FENCE.test(lines[i]) || MD_HEADING.test(lines[i]) || MD_ITEM.test(lines[i]) || mdTableAt(lines, i);

function mdTable(head, rows, inline) {
  const row = (cells, tag) => `<tr>${cells.map(c => `<${tag}>${inline(c)}</${tag}>`).join('')}</tr>`;
  const fit = cells => cells.length > head.length
    ? [...cells.slice(0, head.length - 1), cells.slice(head.length - 1).join(' | ')]  // nothing is dropped
    : head.map((_, n) => cells[n] || '');
  return `<div class="tbl"><table><thead>${row(head, 'th')}</thead><tbody>${rows.map(r => row(fit(r), 'td')).join('')}</tbody></table></div>`;
}

// Items with one level inside them; anything deeper joins that level.
function mdList(items, inline) {
  const open = item => item.ordered ? `<ol${item.start === 1 ? '' : ` start="${item.start}"`}>` : '<ul>';
  const close = item => item.ordered ? '</ol>' : '</ul>';
  let html = open(items[0]), inner = null;
  items.forEach((item, n) => {
    if (item.deep && n) {
      if (!inner) { inner = item; html += open(item); } else html += '</li>';
    } else if (n) {
      if (inner) { html += '</li>' + close(inner); inner = null; }
      html += '</li>';
    }
    html += `<li>${inline(item.text.join('\n'))}`;
  });
  if (inner) html += '</li>' + close(inner);
  return html + '</li>' + close(items[0]);
}

// Headings, lists, tables, code and paragraphs. Whatever is not recognised, or only half there, stays readable text.
function renderMarkdown(value, {link, copyLabel}) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  const inline = text => inlineMd(text, link);
  const out = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = MD_FENCE.exec(line))) {
      const indent = m[1].length, lang = m[2].trim().split(/\s/)[0].slice(0, 30), code = [];
      // A fence that never closes is code to the end.
      for (i++; i < lines.length && lines[i].trim() !== '```'; i++) {
        let cut = 0;
        while (cut < indent && (lines[i][cut] === ' ' || lines[i][cut] === '\t')) cut++;
        code.push(lines[i].slice(cut));
      }
      i++;
      out.push(`<div class="code"><div class="bar"><span>${esc(lang)}</span><button type="button" class="copy">${esc(copyLabel)}</button></div><pre><code>${esc(code.join('\n'))}</code></pre></div>`);
    } else if ((m = MD_HEADING.exec(line))) {
      const level = m[1].length < 3 ? 3 : 4;
      out.push(`<h${level}>${inline(m[2])}</h${level}>`);
      i++;
    } else if (mdTableAt(lines, i)) {
      const head = mdCells(line), rows = [];
      for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes('|'); i++) rows.push(mdCells(lines[i]));
      out.push(mdTable(head, rows, inline));
    } else if ((m = MD_ITEM.exec(line))) {
      const items = [], base = m[1].length;
      while (i < lines.length) {
        const item = MD_ITEM.exec(lines[i]);
        if (item) {
          const deep = item[1].length >= base + 2, ordered = item[2].length > 1;
          if (!deep && items.length && ordered !== items[0].ordered) break;
          items.push({deep, ordered, start: ordered ? parseInt(item[2], 10) : 1, text: [item[3]]});
          i++;
        } else if (!lines[i].trim()) {  // a blank line between two items does not end the list
          let next = i + 1;
          while (next < lines.length && !lines[next].trim()) next++;
          if (!MD_ITEM.test(lines[next] ?? '')) break;
          i = next;
        } else if (/^[ \t]/.test(lines[i]) && !mdStarts(lines, i)) items.at(-1).text.push(lines[i++].trim());
        else break;
      }
      out.push(mdList(items, inline));
    } else {
      const text = [line];
      for (i++; i < lines.length && lines[i].trim() && !mdStarts(lines, i); i++) text.push(lines[i]);
      out.push(`<p>${inline(text.join('\n'))}</p>`);
    }
  }
  return out.join('');
}
