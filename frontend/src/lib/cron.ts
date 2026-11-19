import cronstrue from 'cronstrue/i18n'

/** Format a 5-field cron expression into readable Chinese via `cronstrue`. */
export function formatCronZh(expr: string): string {
  const trimmed = expr.trim()
  if (!trimmed) return ''
  try {
    return cronstrue.toString(trimmed, {
      locale: 'zh_CN',
      use24HourTimeFormat: true,
    })
  } catch {
    return trimmed
  }
}
