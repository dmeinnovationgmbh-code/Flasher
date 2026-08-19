// Thin client for the Python backend (med17flasher webserver).

const json = async (res) => {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export const getVehicle = () => fetch('/api/vehicle').then(json)
export const getMaps = () => fetch('/api/maps').then(json)
export const getTelemetry = () => fetch('/api/telemetry').then(json)
export const getIdentify = () => fetch('/api/identify').then(json)

export const startFlash = (mapId) =>
  fetch('/api/flash', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mapId: mapId ?? null }),
  }).then((res) => res.json())

export const abortFlash = () =>
  fetch('/api/flash/abort', { method: 'POST' }).then(json)

export const buyMap = (id, addon) =>
  fetch(`/api/maps/${id}/buy`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ addon: !!addon }),
  }).then(json)

/* ---- Expert / real flash ------------------------------------------------ */
export const getProfiles = () => fetch('/api/profiles').then(json)
export const getBackends = () => fetch('/api/backends').then(json)
export const getExpert = () => fetch('/api/expert').then(json)

export const uploadFirmware = (file) =>
  fetch(`/api/firmware?name=${encodeURIComponent(file.name)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: file, // raw bytes; the backend parses .bin/.hex/.s19
  }).then(json)

export const setExpertConfig = (cfg) =>
  fetch('/api/expert/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  }).then(json)

export const startExpertFlash = () =>
  fetch('/api/expert/flash', { method: 'POST' }).then((res) => res.json())

/* ---- Diagnostics -------------------------------------------------------- */
export const scanEcu = (deep) => fetch(`/api/scan?deep=${deep ? 1 : 0}`).then(json)
export const readMemory = (address, size) =>
  fetch(`/api/memory?address=${encodeURIComponent(address)}&size=${size}`).then(json)
export const getChecksum = () => fetch('/api/checksum').then(json)
export const correctChecksum = () =>
  fetch('/api/checksum/correct', { method: 'POST' }).then(json)

/* ---- Measurement (XCP) -------------------------------------------------- */
export const getMeasure = () => fetch('/api/measure').then(json)
export const startMeasure = (cfg) =>
  fetch('/api/measure/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg || {}),
  }).then((res) => res.json())
export const stopMeasure = () =>
  fetch('/api/measure/stop', { method: 'POST' }).then(json)
export const measureCsvUrl = () => '/api/measure/csv'

/**
 * Subscribe to the flash event stream (SSE).
 * Returns an unsubscribe function.
 */
export function subscribeFlash(onEvent) {
  const source = new EventSource('/api/flash/stream')
  source.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data))
    } catch {
      /* ignore malformed frames */
    }
  }
  source.onerror = () => {
    // EventSource reconnects on its own; nothing to do here.
  }
  return () => source.close()
}
