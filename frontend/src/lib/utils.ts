/**
 * Parse a search keyword string into {value: type} query dictionary.
 *
 * Input: "keyword:R-18;author_id:12345;tag:NTR;"
 * Output: {"R-18": "keyword", "12345": "author_id", "NTR": "tag"}
 */
export function buildQueries(keyword: string): Record<string, string> {
  const queries: Record<string, string> = {}
  if (!keyword.trim()) return queries

  const conditions = keyword.split(/[;；]/).filter(cond => cond.trim())
  for (const cond of conditions) {
    const trimmed = cond.trim()
    const colonIndex = trimmed.indexOf(':')
    if (colonIndex > 0) {
      const type = trimmed.substring(0, colonIndex).trim()
      const value = trimmed.substring(colonIndex + 1).trim()
      if (value) queries[value] = type
    } else {
      queries[trimmed] = 'keyword'
    }
  }
  return queries
}

/** Format a number: 10000+ displays as x.xw */
export function formatNumber(num?: number): string {
  if (!num) return '0'
  if (num >= 10000) return (num / 10000).toFixed(1) + 'w'
  return num.toString()
}

/** Sanitize a string into a safe filename (replaces illegal chars). */
export function safeFilename(name: string): string {
  return name.replace(/[<>:"/\\|?*]/g, '_').trim() || 'untitled'
}
