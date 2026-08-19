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
