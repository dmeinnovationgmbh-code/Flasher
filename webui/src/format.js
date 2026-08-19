// Formatting helpers mirroring the design's renderVals().

export const LOG_COLORS = {
  '': '#6E6E73',
  ok: '#1F9D4D',
  accent: '#E86E00',
  err: '#D70015',
}

export function fmtAddr(a) {
  const h = (a >>> 0).toString(16).toUpperCase().padStart(8, '0')
  return '0x' + h.slice(0, 4) + ' ' + h.slice(4)
}

export function fmtBytes(done, total) {
  return (
    Math.floor(done).toLocaleString('de-DE') +
    ' / ' +
    total.toLocaleString('de-DE') +
    ' Bytes'
  )
}

export function fmtEta(seconds, running, done) {
  if (done) return '0:00'
  if (!running) return '—'
  const s = Math.max(0, Math.round(seconds))
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') + ' Min'
}

export function nowTime() {
  return new Date().toTimeString().slice(0, 8)
}
