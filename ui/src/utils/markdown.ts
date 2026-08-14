// 轻量 Markdown 渲染（自包含，无外部依赖）
// 供 SkillsView / WikiView 详情共用：把 Markdown 原文渲染成 HTML 片段
// （v-html 使用）。覆盖标题/列表/引用/代码/行内加粗/斜体/链接/行内代码。
// 不做 XSS 白名单之外的富特性——所有文本先 esc 转义，再套标签。

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function inline(t: string): string {
  return esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
}

export function renderMarkdown(content: string): string {
  const lines = content.split('\n')
  const out: string[] = []
  let inCode = false
  let inList = false
  let codeBuf: string[] = []
  for (const raw of lines) {
    const line = raw
    if (line.trim().startsWith('```')) {
      if (inCode) {
        out.push(`<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`)
        codeBuf = []
        inCode = false
      } else {
        inCode = true
      }
      continue
    }
    if (inCode) {
      codeBuf.push(line)
      continue
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) {
      if (inList) { out.push('</ul>'); inList = false }
      const lvl = h[1].length
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`)
      continue
    }
    if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) { out.push('<ul>'); inList = true }
      out.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ''))}</li>`)
      continue
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      if (!inList) { out.push('<ol>'); inList = true }
      out.push(`<li>${inline(line.replace(/^\s*\d+\.\s+/, ''))}</li>`)
      continue
    }
    if (line.trim().startsWith('>')) {
      if (inList) { out.push('</ul>'); inList = false }
      out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ''))}</blockquote>`)
      continue
    }
    if (inList) { out.push('</ul>'); inList = false }
    if (line.trim() === '') {
      continue
    }
    out.push(`<p>${inline(line)}</p>`)
  }
  if (inCode) out.push(`<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`)
  if (inList) out.push('</ul>')
  return out.join('\n')
}

// 清理大段空白：压缩 Markdown 原文中的连续空行（卡片列表摘要用，减空格）
export function collapseBlankLines(content: string, maxBlank = 1): string {
  return content
    .split('\n')
    .reduce<string[]>((acc, line) => {
      if (line.trim() === '') {
        if (acc.length === 0 || acc[acc.length - 1] !== '') acc.push('')
        return acc
      }
      acc.push(line)
      return acc
    }, [])
    .join('\n')
}