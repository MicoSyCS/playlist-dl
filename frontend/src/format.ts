export function pad(n: number, width = 2): string {
  return String(Math.max(0, Math.floor(n))).padStart(width, '0')
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return '—'
  const units = ['b', 'kb', 'mb', 'gb']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function trackProgressLabel(processed: number, total: number): string {
  if (!total) return 'reading playlist'
  return `${processed} of ${total} ${total === 1 ? 'track' : 'tracks'} processed`
}

/** Short, stable job reference for display, e.g. "3f2a-9c". */
export function jobRef(jobId: string): string {
  const id = jobId.replace(/[^a-f0-9]/gi, '').slice(0, 6)
  return `${id.slice(0, 4)}-${id.slice(4)}`
}
