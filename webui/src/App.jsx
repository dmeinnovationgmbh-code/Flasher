import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { LOG_COLORS, fmtAddr, fmtBytes, fmtEta, nowTime } from './format.js'

const ACCENT = '#FF7A00'
const ACCENT_DARK = '#E86E00'
const GREEN = '#1F9D4D'
const MUTED = '#6E6E73'
const FAINT = '#AEAEB2'
const MONO = "ui-monospace,'SF Mono','JetBrains Mono',monospace"

// Where customers get the J2534 driver for a Tactrix Openport 2.0. We do not
// bundle the driver; we point at the vendor's official download page.
const TACTRIX_DRIVER_URL = 'https://www.tactrix.com/index.php?Itemid=61'

const cardStyle = {
  background: '#FFFFFF',
  border: '1px solid rgba(0,0,0,.05)',
  borderRadius: 14,
  boxShadow: '0 1px 2px rgba(0,0,0,.05),0 10px 28px -14px rgba(0,0,0,.1)',
  overflow: 'hidden',
}

const INITIAL_SECTORS = [
  { name: 'SBOOT · 32K', flex: 1, fill: 100, writing: false, done: true },
  { name: 'CBOOT · 192K', flex: 1.6, fill: 0, writing: false, done: false },
  { name: 'ASW · 1.3M', flex: 5, fill: 0, writing: false, done: false },
  { name: 'CAL · 480K', flex: 2.4, fill: 0, writing: false, done: false },
]

export default function App() {
  const [vehicle, setVehicle] = useState(null)
  const [maps, setMaps] = useState([])
  const [addons, setAddons] = useState({})
  const [buying, setBuying] = useState({})
  const [unlocked, setUnlocked] = useState({})

  const [flash, setFlash] = useState({
    running: false, done: false, pct: 0, address: 0x80000000,
    bytesDone: 0, bytesTotal: 0x200000, speed: 0, eta: 0,
    sectors: INITIAL_SECTORS,
  })
  const [volt, setVolt] = useState(13.8)
  const [logs, setLogs] = useState([])
  const [autoscroll, setAutoscroll] = useState(true)
  const [tab, setTab] = useState('flash')
  const [samples, setSamples] = useState([])
  const [measureMeta, setMeasureMeta] = useState(null)
  const [sniffEvents, setSniffEvents] = useState([])
  const [sniffMeta, setSniffMeta] = useState(null)
  const logRef = useRef(null)

  // initial load + telemetry polling
  useEffect(() => {
    api.getVehicle().then((v) => {
      setVehicle(v)
      setVolt(v.volt ?? 13.8)
    }).catch(() => {})
    api.getMaps().then((d) => setMaps(d.maps || [])).catch(() => {})
    setLogs([
      { time: '—', cls: '', msg: 'Interface initialisiert · CAN 500 kBit/s' },
      { time: '—', cls: 'ok', msg: 'ECU antwortet · Diagnosesession erweitert (0x10 03)' },
      { time: '—', cls: '', msg: 'Identifikation gelesen · MED17.7.5 · M177 · C63 S (W205)' },
    ])
    const poll = setInterval(() => {
      api.getTelemetry().then((t) => {
        if (!t.running) setVolt(t.volt)
      }).catch(() => {})
    }, 2400)
    return () => clearInterval(poll)
  }, [])

  // SSE flash stream
  useEffect(() => {
    const unsub = api.subscribeFlash((ev) => {
      if (ev.type === 'hello') {
        setFlash((f) => ({ ...f, running: !!ev.running }))
      } else if (ev.type === 'progress') {
        setVolt(ev.volt)
        setFlash({
          running: ev.running, done: false, pct: ev.pct, address: ev.address,
          bytesDone: ev.bytesDone, bytesTotal: ev.bytesTotal, speed: ev.speed,
          eta: ev.eta, sectors: ev.sectors,
        })
      } else if (ev.type === 'log') {
        setLogs((ls) => ls.concat([{ time: nowTime(), cls: ev.cls, msg: ev.msg }]))
      } else if (ev.type === 'sample') {
        // Keep a bounded window so a long measurement can't grow without limit.
        setSamples((s) => (s.length > 600 ? s.slice(-500) : s).concat([ev]))
      } else if (ev.type === 'measure') {
        if (ev.event === 'started') {
          setSamples([])
          setMeasureMeta({ running: true, signals: ev.signals, mode: ev.mode, backend: ev.backend })
        } else if (ev.event === 'stopped') {
          setMeasureMeta((m) => (m ? { ...m, running: false } : m))
        } else if (ev.event === 'error') {
          setMeasureMeta((m) => ({ ...(m || {}), running: false, error: ev.msg }))
        }
      } else if (ev.type === 'sniff') {
        if (ev.event === 'started') {
          setSniffEvents([])
          setSniffMeta({ running: true, backend: ev.backend, frames: 0, ids: 0, report: null })
        } else if (ev.event === 'flow') {
          setSniffEvents((es) => (es.length > 400 ? es.slice(-350) : es).concat([ev]))
        } else if (ev.event === 'stats') {
          setSniffMeta((m) => ({ ...(m || {}), frames: ev.frames, ids: ev.ids }))
        } else if (ev.event === 'report') {
          setSniffMeta((m) => ({ ...(m || {}), report: ev }))
        } else if (ev.event === 'stopped') {
          setSniffMeta((m) => ({ ...(m || {}), running: false, frames: ev.frames }))
        } else if (ev.event === 'error') {
          setSniffMeta((m) => ({ ...(m || {}), running: false, error: ev.msg }))
        }
      } else if (ev.type === 'done') {
        setFlash((f) => ({
          ...f, running: false, done: true, pct: 100,
          sectors: f.sectors.map((s) => ({ ...s, fill: 100, writing: false, done: true })),
        }))
      } else if (ev.type === 'error') {
        setFlash((f) => ({ ...f, running: false }))
      }
    })
    return unsub
  }, [])

  // log autoscroll
  useEffect(() => {
    if (autoscroll && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs, autoscroll])

  const startFlash = (mapId) => {
    if (flash.running) return
    setFlash((f) => ({ ...f, running: true, done: false, pct: 0 }))
    api.startFlash(mapId).catch(() => {})
  }
  const abortFlash = () => api.abortFlash().catch(() => {})

  // Export the protocol pane as a text file (the "Exportieren" action).
  const exportLog = () => {
    const text = logs.map((l) => `${l.time}\t${l.msg}`).join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'med17-protokoll.txt'
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  // Replace the placeholder vehicle data with what the ECU actually reports.
  const applyIdent = (dids) => {
    const pick = (needle) => dids.find((d) =>
      (d.name || '').toLowerCase().includes(needle))?.value
    setVehicle((v) => ({
      ...v,
      vin: pick('vin') || v.vin,
      swNumber: pick('sw number') || pick('sw version') || v.swNumber,
      hwNumber: pick('hw number') || pick('hw version') || v.hwNumber,
      identified: true,
    }))
  }

  const buy = (m) => {
    if (buying[m.id]) return
    setBuying((b) => ({ ...b, [m.id]: true }))
    api.buyMap(m.id, !!addons[m.id])
      .then(() => {
        setBuying((b) => ({ ...b, [m.id]: false }))
        setUnlocked((u) => ({ ...u, [m.id]: true }))
      })
      .catch(() => setBuying((b) => ({ ...b, [m.id]: false })))
  }

  if (!vehicle) return <div style={{ padding: 40, color: MUTED }}>Lädt …</div>

  const running = flash.running
  const done = flash.done
  const pct = flash.pct
  const p = Math.round(pct)
  const badgeText = running ? 'Schreiben aktiv' : done ? 'Abgeschlossen' : 'Bereit'
  const badgeColor = running ? ACCENT_DARK : done ? GREEN : MUTED

  return (
    <>
      <Header voltText={volt.toFixed(1) + ' V'} bus={vehicle.connection.bus} />

      <VehicleBar vehicle={vehicle} running={running}
        writeLabel={running ? 'Läuft …' : 'Schreiben'} onWrite={() => startFlash(null)}
        onRead={() => setTab('diag')} />

      <TabBar tab={tab} setTab={setTab} mapCount={maps.length} />

      <main style={{
        display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 324px', gap: 18,
        maxWidth: 1180, margin: '0 auto', padding: '14px 22px 56px', alignItems: 'start',
      }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
          {tab === 'flash' && (
            <>
              <ExpertSection running={running} />
              <WriteSection flash={flash} p={p} badgeText={badgeText} badgeColor={badgeColor}
                running={running} onAbort={abortFlash} file={vehicle.file} />
              <LogSection logs={logs} logRef={logRef} autoscroll={autoscroll}
                onToggle={() => setAutoscroll((a) => !a)} onExport={exportLog} />
            </>
          )}
          {tab === 'measure' && <MeasureSection samples={samples} meta={measureMeta} />}
          {tab === 'sniff' && <SniffSection events={sniffEvents} meta={sniffMeta} />}
          {tab === 'diag' && <DiagSection onIdentified={applyIdent} />}
          {tab === 'maps' && (
            <MapsSection maps={maps} addons={addons} setAddons={setAddons}
              buying={buying} unlocked={unlocked} onBuy={buy}
              onFlash={(id) => { setTab('flash'); startFlash(id) }} />
          )}
        </div>

        <Sidebar vehicle={vehicle} onTool={(k) => setTab(k)} />
      </main>
    </>
  )
}

/* ---------------------------------------------------------------- TabBar */
function TabBar({ tab, setTab, mapCount }) {
  const tabs = [
    ['flash', 'Flashen'],
    ['measure', 'Messen'],
    ['sniff', 'Sniffer'],
    ['diag', 'Diagnose'],
    ['maps', `OTS-Maps${mapCount ? ' · ' + mapCount : ''}`],
  ]
  return (
    <div style={{ maxWidth: 1180, margin: '0 auto', padding: '0 22px', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {tabs.map(([k, label]) => {
        const on = tab === k
        return (
          <button key={k} onClick={() => setTab(k)} style={{
            padding: '8px 18px', borderRadius: 9, fontSize: 13.5, fontWeight: 600,
            background: on ? '#1D1D1F' : 'rgba(0,0,0,.05)', color: on ? '#FFFFFF' : MUTED,
            transition: 'background .15s',
          }}>{label}</button>
        )
      })}
    </div>
  )
}

/* ---------------------------------------------------------------- Header */
function Header({ voltText, bus }) {
  return (
    <header style={{
      display: 'flex', alignItems: 'center', gap: 14, height: 52, padding: '0 22px',
      position: 'sticky', top: 0, zIndex: 50, background: 'rgba(255,255,255,.72)',
      backdropFilter: 'blur(20px) saturate(180%)', WebkitBackdropFilter: 'blur(20px) saturate(180%)',
      borderBottom: '1px solid rgba(0,0,0,.06)',
    }}>
      <img src="./assets/dme-logo.svg" alt="DME Innovation" style={{ height: 22, width: 'auto' }} />
      <div style={{ width: 1, height: 18, background: 'rgba(0,0,0,.1)' }} />
      {/* The logo already reads "DME Innovation"; together they say the full
          product name without printing the company twice. */}
      <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.04em', whiteSpace: 'nowrap', textTransform: 'uppercase' }}>
        MED17 Flasher
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '5px 12px', borderRadius: 99, background: 'rgba(0,0,0,.04)', fontSize: 12, fontWeight: 500 }}>
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#34C759', animation: 'pulse 2.4s ease-in-out infinite' }} />
          Verbunden · {bus}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', padding: '5px 12px', borderRadius: 99, background: 'rgba(0,0,0,.04)', fontFamily: MONO, fontSize: 12, color: GREEN, fontVariantNumeric: 'tabular-nums' }}>
          {voltText}
        </div>
        <div style={{ width: 30, height: 30, borderRadius: '50%', background: 'rgba(0,0,0,.06)', display: 'grid', placeItems: 'center', fontSize: 11.5, fontWeight: 600 }}>DM</div>
      </div>
    </header>
  )
}

/* ------------------------------------------------------------ VehicleBar */
function VehicleBar({ vehicle, running, writeLabel, onWrite, onRead }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 24, flexWrap: 'wrap', maxWidth: 1180, margin: '0 auto', padding: '34px 22px 24px' }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: MUTED, letterSpacing: '.01em', marginBottom: 4 }}>Verbundenes Fahrzeug</div>
        <h1 style={{ fontFamily: "'Titillium Web',sans-serif", fontSize: 32, fontWeight: 700, letterSpacing: '-.022em', lineHeight: 1.15 }}>
          {vehicle.name} <span style={{ color: FAINT, fontWeight: 500 }}>{vehicle.chassis}</span>
        </h1>
        <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginTop: 7, fontFamily: MONO, fontSize: 12, color: MUTED }}>
          <span>{vehicle.engine}</span>
          <span>{vehicle.ecu}</span>
          <span style={{ color: FAINT }}>VIN {vehicle.vin}</span>
        </div>
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <button className="btn-secondary" onClick={onRead} style={{ padding: '10px 22px', borderRadius: 9, background: '#FFFFFF', border: '1px solid rgba(0,0,0,.12)', fontSize: 14, fontWeight: 600 }}>Lesen</button>
        <button className="btn-primary" onClick={onWrite} disabled={running}
          style={{ padding: '10px 24px', borderRadius: 9, background: ACCENT, color: '#FFFFFF', fontSize: 14, fontWeight: 700, letterSpacing: '.01em', boxShadow: '0 4px 12px -4px rgba(255,122,0,.5)' }}>
          {writeLabel}
        </button>
      </div>
    </div>
  )
}

/* ----------------------------------------------------------- WriteSection */
function WriteSection({ flash, p, badgeText, badgeColor, running, onAbort, file }) {
  return (
    <section style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 0' }}>
        <span style={{ fontSize: 16, fontWeight: 700 }}>Schreibvorgang</span>
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, fontWeight: 500, color: badgeColor }}>
          {running && <span style={{ width: 7, height: 7, borderRadius: '50%', background: ACCENT, animation: 'pulse 1.2s ease-in-out infinite' }} />}
          {badgeText}
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 20px 0', fontFamily: MONO, fontSize: 11.5, color: MUTED }}>
        {file.name} · {file.size}
        <span style={{ display: 'flex', alignItems: 'center', gap: 4, color: GREEN }}>
          <Check w={12} stroke={GREEN} sw={2.2} />Prüfsumme {file.checksum}
        </span>
      </div>
      <div style={{ padding: '18px 20px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 18 }}>
          <span style={{ fontFamily: "'Titillium Web',sans-serif", fontSize: 46, fontWeight: 700, letterSpacing: '-.02em', lineHeight: 1, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap', flexShrink: 0 }}>{p} %</span>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontFamily: MONO, fontSize: 12, color: MUTED, fontVariantNumeric: 'tabular-nums' }}>
            <span style={{ whiteSpace: 'nowrap' }}>Adresse <span style={{ color: ACCENT_DARK }}>{fmtAddr(flash.address)}</span></span>
            <span style={{ whiteSpace: 'nowrap' }}>Sektor {flash.pct < 78 ? 'ASW 3/4' : 'CAL 1/1'}</span>
            <span style={{ whiteSpace: 'nowrap' }}>{(running ? flash.speed : 0)} KB/s</span>
            <span style={{ whiteSpace: 'nowrap' }}>Restzeit {fmtEta(flash.eta, running, flash.done)}</span>
          </div>
        </div>
        <div style={{ height: 5, borderRadius: 99, background: '#E8E8ED', overflow: 'hidden', marginTop: 14 }}>
          <div style={{ height: '100%', borderRadius: 99, background: ACCENT, transition: 'width .3s linear', width: flash.pct + '%' }} />
        </div>

        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10, margin: '22px 0 8px' }}>
          <span style={{ fontSize: 12.5, fontWeight: 600, color: MUTED }}>PFLASH-Belegung · TriCore TC1767</span>
          <span style={{ fontFamily: MONO, fontSize: 11.5, color: FAINT, fontVariantNumeric: 'tabular-nums' }}>{fmtBytes(flash.bytesDone, flash.bytesTotal)}</span>
        </div>
        <div style={{ display: 'flex', gap: 3, height: 40 }}>
          {flash.sectors.map((s, i) => {
            const nameColor = s.done || s.fill > 30 ? '#fff' : MUTED
            return (
              <div key={i} style={{ position: 'relative', borderRadius: 8, overflow: 'hidden', background: '#E8E8ED', flex: s.flex }}>
                <div style={{ position: 'absolute', inset: 0, background: ACCENT, width: s.fill + '%' }} />
                {s.writing && (
                  <div style={{ position: 'absolute', inset: 0, overflow: 'hidden' }}>
                    <div style={{ position: 'absolute', top: 0, bottom: 0, width: '40%', background: 'linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent)', animation: 'scan 1.6s linear infinite' }} />
                  </div>
                )}
                <span style={{ position: 'absolute', left: 8, bottom: 5, zIndex: 2, fontFamily: MONO, fontSize: 10, color: nameColor, whiteSpace: 'nowrap' }}>{s.name}</span>
              </div>
            )
          })}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: MONO, fontSize: 10, color: FAINT, marginTop: 5 }}>
          <span>0x8000 0000</span><span>0x8008 0000</span><span>0x8010 0000</span><span>0x8018 0000</span><span>0x801F FFFF</span>
        </div>

        {running && (
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginTop: 16, fontSize: 12, color: MUTED, animation: 'fadeSlide .3s ease both' }}>
            <Warn />
            <span>Zündung eingeschaltet lassen und Vorgang nicht unterbrechen. Ladegerät empfohlen — automatischer Abbruch unter 11,5 V.</span>
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, marginTop: 18 }}>
          <button className="btn-cancel" onClick={onAbort} disabled={!running}
            style={{ padding: '8px 18px', borderRadius: 8, fontSize: 13, fontWeight: 600, background: 'rgba(0,0,0,.05)', color: running ? '#D70015' : FAINT }}>Abbrechen</button>
        </div>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------ MapsSection */
function MapsSection({ maps, addons, setAddons, buying, unlocked, onBuy, onFlash }) {
  return (
    <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10, padding: '0 4px' }}>
        <span style={{ fontSize: 16, fontWeight: 700 }}>OTS-Maps für dieses Fahrzeug</span>
        <span style={{ fontSize: 12, color: FAINT }}>{maps.length} Treffer · SW 1779032500</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(290px,1fr))', gap: 14 }}>
        {maps.map((m) => {
          const isUnlocked = !!unlocked[m.id] || m.unlocked
          const isBuying = !!buying[m.id]
          const addon = !!addons[m.id]
          const total = m.base + (addon ? 100 : 0)
          return (
            <div key={m.id} style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 20, border: `1px solid ${isUnlocked ? 'rgba(52,199,89,.5)' : 'rgba(0,0,0,.05)'}`, borderRadius: 18, background: '#FFFFFF', boxShadow: '0 1px 3px rgba(0,0,0,.04)' }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10 }}>
                <div>
                  <div style={{ fontSize: 17, fontWeight: 600, letterSpacing: '-.01em' }}>{m.name}</div>
                  <div style={{ fontSize: 12.5, color: MUTED, marginTop: 2 }}>{m.sub}</div>
                </div>
                <div style={{ fontSize: 15, fontWeight: 600, color: '#1D1D1F', whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>{isUnlocked ? '' : total + ' €'}</div>
              </div>
              <div style={{ display: 'flex', gap: 22 }}>
                <div>
                  <div style={{ fontFamily: "'Titillium Web',sans-serif", fontSize: 22, fontWeight: 600, letterSpacing: '-.02em', fontVariantNumeric: 'tabular-nums' }}>{m.ps} PS</div>
                  <div style={{ fontSize: 11.5, color: GREEN, fontWeight: 500 }}>{m.psGain} PS</div>
                </div>
                <div>
                  <div style={{ fontFamily: "'Titillium Web',sans-serif", fontSize: 22, fontWeight: 600, letterSpacing: '-.02em', fontVariantNumeric: 'tabular-nums' }}>{m.nm} Nm</div>
                  <div style={{ fontSize: 11.5, color: GREEN, fontWeight: 500 }}>{m.nmGain} Nm</div>
                </div>
              </div>
              <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12.5, color: MUTED }}>
                {m.features.map((f, i) => (
                  <li key={i} style={{ display: 'flex', gap: 7, alignItems: 'baseline' }}>
                    <Check w={10} stroke={FAINT} sw={2.4} top={1} />{f}
                  </li>
                ))}
              </ul>

              {(!isUnlocked || addon) && (
                <button onClick={() => { if (!isBuying && !isUnlocked) setAddons((a) => ({ ...a, [m.id]: !a[m.id] })) }}
                  style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0', borderTop: '1px solid rgba(0,0,0,.05)', textAlign: 'left', width: '100%' }}>
                  <span style={{ display: 'flex', flexDirection: 'column', flex: 1 }}>
                    <span style={{ fontSize: 13, fontWeight: 500 }}>Pops &amp; Bangs</span>
                    <span style={{ fontSize: 11.5, color: FAINT }}>in Sport+ / Race · +100 €</span>
                  </span>
                  <Switch on={addon} onColor={ACCENT} w={38} />
                </button>
              )}

              {isUnlocked ? (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: GREEN, fontWeight: 500, animation: 'fadeSlide .35s ease both' }}>
                    <CheckCircle />Freigeschaltet für VIN …4112
                  </div>
                  <button className="btn-flash-ecu" onClick={() => onFlash(m.id)}
                    style={{ width: '100%', padding: 10, borderRadius: 9, background: '#1D1D1F', color: '#FFFFFF', fontWeight: 600, fontSize: 13.5 }}>Auf ECU flashen</button>
                </>
              ) : (
                <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 7 }}>
                  <button className="btn-buy" onClick={() => onBuy(m)} disabled={isBuying}
                    style={{ width: '100%', padding: 10, borderRadius: 9, background: isBuying ? 'rgba(0,0,0,.05)' : ACCENT, boxShadow: '0 4px 12px -6px rgba(255,122,0,.4)', color: isBuying ? FAINT : '#FFFFFF', fontWeight: 600, fontSize: 13.5 }}>
                    {isBuying ? 'Weiterleitung zu Stripe …' : 'Kaufen · ' + total + ' €'}
                  </button>
                  <span style={{ fontSize: 11, color: FAINT, textAlign: 'center' }}>Sichere Zahlung über Stripe · Freischaltung per VIN</span>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

/* ---------------------------------------------------------- ExpertSection */
const fieldLabel = { fontSize: 12, fontWeight: 600, color: MUTED, marginBottom: 5, display: 'block' }
const inputStyle = {
  width: '100%', padding: '9px 11px', borderRadius: 8, border: '1px solid rgba(0,0,0,.14)',
  background: '#FFFFFF', fontSize: 13, color: '#1D1D1F', fontFamily: 'inherit',
}

function ExpertSection({ running }) {
  const [profiles, setProfiles] = useState([])
  const [backends, setBackends] = useState([])
  const [form, setForm] = useState({
    profileId: '', backend: 'simulator', seedSource: 'profile',
    seedUrl: '', seedPath: '', seedOptions: '', python32: '', allowWrite: false,
    repoUrl: '', repoToken: '',
  })
  const [fw, setFw] = useState(null)
  const [repoList, setRepoList] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [pre, setPre] = useState(null)

  useEffect(() => {
    api.getProfiles().then((d) => {
      const ps = d.profiles || []
      setProfiles(ps)
      setForm((f) => ({
        ...f,
        profileId: f.profileId || (ps.find((p) => p.id.includes('med1775'))?.id || ps[0]?.id || ''),
      }))
    }).catch(() => {})
    api.getBackends().then((d) => setBackends(d.backends || [])).catch(() => {})
    api.getExpert().then((c) => { if (c.firmware) setFw(c.firmware) }).catch(() => {})
  }, [])

  const upd = (k, v) => { setPre(null); setForm((f) => ({ ...f, [k]: v })) }
  const backendObj = backends.find((b) => b.id === form.backend)
  const isReal = backendObj ? backendObj.real : false

  const onUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setBusy(true); setMsg(null)
    try {
      const d = await api.uploadFirmware(file)
      setFw(d.firmware)
      setMsg({ ok: true, t: `Firmware geladen · ${d.firmware.name} · ${d.firmware.programBytes} Bytes · CRC32 ${d.firmware.crc32}` })
    } catch {
      setMsg({ ok: false, t: 'Upload/Parsing fehlgeschlagen — .bin / .hex / .s19 erwartet.' })
    } finally {
      setBusy(false)
    }
  }

  const onRepoConnect = async () => {
    setBusy(true); setMsg(null)
    try {
      const cfg = await api.setRepo({ url: form.repoUrl, token: form.repoToken })
      if (!cfg.url) { setRepoList(null); setMsg({ ok: true, t: 'Firmware-Server getrennt.' }); return }
      if (cfg.reachable === false) {
        setRepoList(null)
        setMsg({ ok: false, t: `Server nicht erreichbar: ${cfg.error || 'keine Antwort'}` })
        return
      }
      const d = await api.listRepo()
      setRepoList(d.firmwares || [])
      setMsg({ ok: true, t: `Verbunden · ${(d.firmwares || []).length} Firmware(s) im Katalog.` })
    } catch (err) {
      setRepoList(null)
      setMsg({ ok: false, t: String(err.message || err) })
    } finally { setBusy(false) }
  }

  const onRepoUse = async (id) => {
    setBusy(true); setMsg(null)
    try {
      const d = await api.useRepoFirmware(id)
      setFw(d.firmware)
      setMsg({ ok: true, t: `Vom Server geladen · ${d.firmware.name} · ${d.firmware.programBytes} Bytes · CRC32 ${d.firmware.crc32}` })
    } catch (err) {
      setMsg({ ok: false, t: `Download fehlgeschlagen: ${String(err.message || err)}` })
    } finally { setBusy(false) }
  }

  const seedCfg = () => {
    const s = form.seedSource
    if (s === 'server') return { source: 'server', url: form.seedUrl }
    if (s === 'dll' || s === 'bridge' || s === 'exe')
      return { source: s, path: form.seedPath, options: form.seedOptions, python32: form.python32 }
    if (s === 'store') return { source: 'store', path: form.seedPath }
    return { source: 'profile' }
  }

  const onPreflight = async () => {
    setBusy(true); setMsg(null); setPre(null)
    try {
      await api.setExpertConfig({
        profileId: form.profileId, backend: form.backend,
        seedkey: seedCfg(), allowWrite: form.allowWrite,
      })
      const r = await api.preflightExpert()
      setPre(r)
      setMsg({ ok: r.ok, t: r.ok
        ? `Probelauf bestanden · ${r.summary} · nichts geschrieben`
        : `Probelauf fehlgeschlagen · ${r.summary} · NICHT flashen` })
    } catch (err) {
      setMsg({ ok: false, t: `Probelauf-Fehler: ${String(err.message || err)}` })
    } finally { setBusy(false) }
  }

  const onFlash = async () => {
    setBusy(true); setMsg(null)
    try {
      await api.setExpertConfig({
        profileId: form.profileId, backend: form.backend,
        seedkey: seedCfg(), allowWrite: form.allowWrite,
      })
      const r = await api.startExpertFlash()
      if (r.started) setMsg({ ok: true, t: 'Echt-Flash gestartet — Fortschritt oben im Schreibvorgang.' })
      else setMsg({ ok: false, t: r.error || 'Es läuft bereits ein Schreibvorgang.' })
    } catch (err) {
      setMsg({ ok: false, t: String(err.message || err) })
    } finally {
      setBusy(false)
    }
  }

  const preOk = pre && pre.ok
  const canPreflight = form.profileId && fw && !running && !busy
  // A real write additionally requires a passed rehearsal for THIS config.
  const canFlash = canPreflight && (!isReal || (form.allowWrite && preOk))
  const seedNeedsUrl = form.seedSource === 'server'
  const seedNeedsPath = ['dll', 'bridge', 'exe', 'store'].includes(form.seedSource)

  return (
    <section style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 0' }}>
        <span style={{ fontSize: 16, fontWeight: 700 }}>Experte · Echt-Flash</span>
        <span style={{ marginLeft: 'auto', fontSize: 12, color: FAINT }}>eigene Datei · echte UDS-Sequenz</span>
      </div>
      <div style={{ padding: '4px 20px 0', fontSize: 12, color: MUTED }}>
        Eigene Firmware auf ein gewähltes Profil (z. B. MED17.7.5&nbsp;med1775) flashen — Simulator oder echter CAN-Adapter, mit Seed/Key aus Profil, Server oder DLL.
      </div>
      <div style={{ padding: '16px 20px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div>
            <label style={fieldLabel}>Profil</label>
            <select style={inputStyle} value={form.profileId} onChange={(e) => upd('profileId', e.target.value)}>
              {profiles.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
            </select>
          </div>
          <div>
            <label style={fieldLabel}>Verbindung</label>
            <select style={inputStyle} value={form.backend} onChange={(e) => upd('backend', e.target.value)}>
              {backends.map((b) => (
                <option key={b.id} value={b.id} disabled={!b.available}>
                  {b.name}{b.available ? '' : ' (nicht verfügbar)'}
                </option>
              ))}
            </select>
            {!backends.some((b) => String(b.id).startsWith('j2534') && b.available) && (
              <div style={{ fontSize: 11.5, color: MUTED, marginTop: 6, lineHeight: 1.4 }}>
                Kein J2534-Gerät gefunden. Für einen Tactrix Openport 2.0 zuerst den
                Treiber installieren:{' '}
                <a href={TACTRIX_DRIVER_URL} target="_blank" rel="noreferrer"
                  style={{ color: ACCENT, fontWeight: 600 }}>
                  Tactrix-Treiber herunterladen
                </a>
              </div>
            )}
          </div>
        </div>

        <div style={{ border: '1px solid rgba(0,0,0,.09)', borderRadius: 10, padding: '12px 14px' }}>
          <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 8 }}>Firmware vom Server</div>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr auto', gap: 8, alignItems: 'end' }}>
            <div>
              <label style={fieldLabel}>Server-URL</label>
              <input style={inputStyle} placeholder="http://192.168.1.10:8080"
                value={form.repoUrl} onChange={(e) => upd('repoUrl', e.target.value)} />
            </div>
            <div>
              <label style={fieldLabel}>Token (optional)</label>
              <input style={inputStyle} type="password" placeholder="Bearer-Token"
                value={form.repoToken} onChange={(e) => upd('repoToken', e.target.value)} />
            </div>
            <button onClick={onRepoConnect} disabled={busy}
              style={{ padding: '8px 16px', borderRadius: 8, fontSize: 13, fontWeight: 600, border: '1px solid rgba(0,0,0,.12)' }}>
              Verbinden
            </button>
          </div>
          {repoList && (
            repoList.length === 0
              ? <div style={{ marginTop: 8, fontSize: 12, color: MUTED }}>Der Katalog ist leer.</div>
              : (
                <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 190, overflowY: 'auto' }}>
                  {repoList.map((f) => (
                    <div key={f.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 10px', borderRadius: 8, background: 'rgba(0,0,0,.04)' }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ fontSize: 12.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.filename}</div>
                        <div style={{ fontFamily: MONO, fontSize: 11, color: FAINT }}>
                          {f.ecu || '—'}{f.sw_version ? ` · SW ${f.sw_version}` : ''} · {f.size} B
                        </div>
                      </div>
                      <button onClick={() => onRepoUse(f.id)} disabled={busy}
                        style={{ padding: '5px 12px', borderRadius: 7, fontSize: 12, fontWeight: 600, border: '1px solid rgba(0,0,0,.12)' }}>
                        Laden
                      </button>
                    </div>
                  ))}
                </div>
              )
          )}
        </div>

        <div>
          <label style={fieldLabel}>… oder Firmware-Datei vom Rechner</label>
          <input type="file" accept=".bin,.hex,.s19,.srec,.mot" onChange={onUpload}
            style={{ ...inputStyle, padding: '7px 10px' }} />
          {fw && (
            <div style={{ marginTop: 6, fontFamily: MONO, fontSize: 11.5, color: GREEN }}>
              {fw.name} · {fw.programBytes} B · CRC32 {fw.crc32}
              {fw.span && <> · {fw.span[0]}–{fw.span[1]}</>}
            </div>
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: seedNeedsUrl || seedNeedsPath ? '1fr 1fr' : '1fr', gap: 12 }}>
          <div>
            <label style={fieldLabel}>Seed/Key</label>
            <select style={inputStyle} value={form.seedSource} onChange={(e) => upd('seedSource', e.target.value)}>
              <option value="profile">Aus Profil (Algorithmus)</option>
              <option value="server">Seed/Key-Server (URL)</option>
              <option value="dll">Vendor-DLL (32-Bit automatisch)</option>
              <option value="bridge">Vendor-DLL über 32-Bit-Helfer (erzwungen)</option>
              <option value="exe">Seed/Key-EXE</option>
              <option value="store">Seed/Key-Katalog (JSON)</option>
            </select>
          </div>
          {seedNeedsUrl && (
            <div>
              <label style={fieldLabel}>Server-URL</label>
              <input style={inputStyle} placeholder="http://127.0.0.1:8377"
                value={form.seedUrl} onChange={(e) => upd('seedUrl', e.target.value)} />
            </div>
          )}
          {seedNeedsPath && (
            <div>
              <label style={fieldLabel}>{form.seedSource === 'store' ? 'JSON-Pfad' : 'Pfad zur DLL/EXE'}</label>
              <input style={inputStyle} placeholder={form.seedSource === 'store' ? 'seedkeys.json' : 'MED1775_12_42_00.dll'}
                value={form.seedPath} onChange={(e) => upd('seedPath', e.target.value)} />
            </div>
          )}
        </div>

        {isReal && (
          <label style={{ display: 'flex', alignItems: 'flex-start', gap: 9, padding: '10px 12px', borderRadius: 9, background: 'rgba(255,122,0,.08)', border: '1px solid rgba(255,122,0,.25)', cursor: 'pointer' }}>
            <input type="checkbox" checked={form.allowWrite} onChange={(e) => upd('allowWrite', e.target.checked)}
              style={{ marginTop: 2 }} />
            <span style={{ fontSize: 12, color: '#8A4B00' }}>
              <b>Schreiben auf echte Hardware freigeben.</b> Zündung ein, stabile Spannung, korrektes Profil/Datei/Seed-Key. Ein falscher Flash kann das Steuergerät unbrauchbar machen.
            </span>
          </label>
        )}

        {pre && (
          <div style={{ border: `1px solid ${pre.ok ? 'rgba(52,199,89,.4)' : 'rgba(215,0,21,.4)'}`, borderRadius: 10, padding: '10px 12px', background: pre.ok ? 'rgba(52,199,89,.06)' : 'rgba(215,0,21,.05)' }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 6, color: pre.ok ? GREEN : '#D70015' }}>
              Probelauf {pre.ok ? 'bestanden' : 'fehlgeschlagen'} · {pre.summary}{pre.simulator ? ' · Simulator' : ''}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              {pre.checks.map((c, i) => (
                <div key={i} style={{ fontSize: 11.5, fontFamily: MONO, color: c.ok ? MUTED : (c.fatal ? '#D70015' : '#8A4B00') }}>
                  {c.ok ? '✓' : (c.fatal ? '✕' : '!')} {c.name}{c.detail ? ` — ${c.detail}` : ''}
                </div>
              ))}
            </div>
          </div>
        )}

        {msg && (
          <div style={{ fontSize: 12, color: msg.ok ? GREEN : '#D70015' }}>{msg.t}</div>
        )}

        {isReal && !preOk && (
          <div style={{ fontSize: 11.5, color: FAINT }}>
            Vor dem echten Schreiben ist ein bestandener Probelauf erforderlich.
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <button onClick={onPreflight} disabled={!canPreflight}
            style={{ padding: '10px 18px', borderRadius: 9, fontSize: 14, fontWeight: 600, border: '1px solid rgba(0,0,0,.15)', background: canPreflight ? '#fff' : 'rgba(0,0,0,.05)', color: canPreflight ? '#111' : FAINT }}>
            Probelauf (nichts schreiben)
          </button>
          <button onClick={onFlash} disabled={!canFlash}
            style={{ padding: '10px 22px', borderRadius: 9, fontSize: 14, fontWeight: 700, color: '#fff', background: canFlash ? ACCENT : 'rgba(0,0,0,.15)', boxShadow: canFlash ? '0 4px 12px -4px rgba(255,122,0,.5)' : 'none' }}>
            {isReal ? 'Echt flashen' : 'Im Simulator flashen'}
          </button>
          <span style={{ fontSize: 11.5, color: FAINT }}>
            {isReal ? 'Nutzt den echten CAN-Adapter.' : 'Sicher: virtuelle ECU, kein Schreiben auf Hardware.'}
          </span>
        </div>
      </div>
    </section>
  )
}

/* --------------------------------------------------------- MeasureSection */
const SERIES_COLORS = [ACCENT, '#0A84FF', GREEN, '#AF52DE', '#FF375F', '#FFB300']

function Sparkline({ points, color, height = 54 }) {
  if (points.length < 2) {
    return <div style={{ height, display: 'grid', placeItems: 'center', fontSize: 11, color: FAINT }}>—</div>
  }
  const lo = Math.min(...points)
  const hi = Math.max(...points)
  const span = hi - lo || 1
  const step = 100 / (points.length - 1)
  const d = points
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(2)},${(100 - ((v - lo) / span) * 100).toFixed(2)}`)
    .join(' ')
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none"
      style={{ width: '100%', height, display: 'block' }}>
      <path d={d} fill="none" stroke={color} strokeWidth="2"
        vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function MeasureSection({ samples, meta }) {
  const [backend, setBackend] = useState('simulator')
  const [backends, setBackends] = useState([])
  const [rate, setRate] = useState(10)
  const [daq, setDaq] = useState(false)
  const [specs, setSpecs] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    api.getBackends().then((d) => setBackends(d.backends || [])).catch(() => {})
    api.getMeasure().then((c) => {
      setSpecs((c.signals || []).join('\n'))
      setBackend(c.backend || 'simulator')
      setRate(c.rate || 10)
    }).catch(() => {})
  }, [])

  const running = !!meta?.running
  const names = (meta?.signals || []).map((s) => s.name)
  const units = Object.fromEntries((meta?.signals || []).map((s) => [s.name, s.unit || '']))
  const latest = samples.length ? samples[samples.length - 1] : null

  const start = async () => {
    setBusy(true); setErr(null)
    try {
      const list = specs.split('\n').map((s) => s.trim()).filter(Boolean)
      const r = await api.startMeasure({ signals: list, backend, rate: Number(rate), daq })
      if (!r.started) setErr(r.error || 'Messung läuft bereits.')
    } catch (e) { setErr(String(e.message || e)) } finally { setBusy(false) }
  }
  const stop = () => api.stopMeasure().catch(() => {})

  return (
    <>
      <section style={cardStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 0' }}>
          <span style={{ fontSize: 16, fontWeight: 700 }}>Messen · XCP</span>
          <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: running ? ACCENT_DARK : MUTED }}>
            {running && <span style={{ width: 7, height: 7, borderRadius: '50%', background: ACCENT, animation: 'pulse 1.2s ease-in-out infinite' }} />}
            {running ? `Läuft · ${meta.mode === 'daq' ? 'DAQ' : 'Polling'}` : 'Bereit'}
          </span>
        </div>
        <div style={{ padding: '4px 20px 0', fontSize: 12, color: MUTED }}>
          Live-Werte aus dem Steuergerät lesen und aufzeichnen. Signale als <code>name@0xAdresse:typ:faktor:offset:einheit</code>, eines pro Zeile.
        </div>
        <div style={{ padding: '16px 20px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr', gap: 12 }}>
            <div>
              <label style={fieldLabel}>Verbindung</label>
              <select style={inputStyle} value={backend} disabled={running}
                onChange={(e) => setBackend(e.target.value)}>
                {backends.map((b) => (
                  <option key={b.id} value={b.id} disabled={!b.available}>
                    {b.name}{b.available ? '' : ' (nicht verfügbar)'}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label style={fieldLabel}>Rate (Hz)</label>
              <input style={inputStyle} type="number" min="1" max="200" value={rate}
                disabled={running || daq} onChange={(e) => setRate(e.target.value)} />
            </div>
            <div>
              <label style={fieldLabel}>Modus</label>
              <select style={inputStyle} value={daq ? 'daq' : 'poll'} disabled={running}
                onChange={(e) => setDaq(e.target.value === 'daq')}>
                <option value="poll">Polling</option>
                <option value="daq">DAQ (Streaming)</option>
              </select>
            </div>
          </div>
          <div>
            <label style={fieldLabel}>Signale</label>
            <textarea style={{ ...inputStyle, minHeight: 78, fontFamily: MONO, fontSize: 12 }}
              value={specs} disabled={running} onChange={(e) => setSpecs(e.target.value)}
              placeholder="rpm@0x2000:u16&#10;coolant@0x2002:s16:0.1:-40:degC" />
          </div>
          {err && <div style={{ fontSize: 12, color: '#D70015' }}>{err}</div>}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button onClick={running ? stop : start} disabled={busy}
              style={{ padding: '10px 22px', borderRadius: 9, fontSize: 14, fontWeight: 700, color: '#fff', background: running ? '#D70015' : ACCENT, boxShadow: running ? 'none' : '0 4px 12px -4px rgba(255,122,0,.5)' }}>
              {running ? 'Stoppen' : 'Messung starten'}
            </button>
            <a href={api.measureCsvUrl()} download="messung.csv"
              style={{ padding: '9px 18px', borderRadius: 8, fontSize: 13, fontWeight: 600, background: 'rgba(0,0,0,.05)', color: samples.length ? '#1D1D1F' : FAINT, textDecoration: 'none', pointerEvents: samples.length ? 'auto' : 'none' }}>
              CSV exportieren
            </a>
            <span style={{ marginLeft: 'auto', fontFamily: MONO, fontSize: 11.5, color: FAINT }}>
              {samples.length} Messpunkte
            </span>
          </div>
        </div>
      </section>

      {names.length > 0 && (
        <section style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 14 }}>
          {names.map((n, i) => {
            const color = SERIES_COLORS[i % SERIES_COLORS.length]
            const series = samples.map((s) => s.values[n]).filter((v) => typeof v === 'number')
            const val = latest?.values?.[n]
            return (
              <div key={n} style={{ ...cardStyle, padding: '14px 16px 8px' }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: MUTED }}>{n}</span>
                  <span style={{ marginLeft: 'auto', fontFamily: "'Titillium Web',sans-serif", fontSize: 26, fontWeight: 700, letterSpacing: '-.02em', fontVariantNumeric: 'tabular-nums', color }}>
                    {typeof val === 'number' ? val.toFixed(2) : '—'}
                  </span>
                  <span style={{ fontSize: 11.5, color: FAINT }}>{units[n]}</span>
                </div>
                <Sparkline points={series.slice(-160)} color={color} />
              </div>
            )
          })}
        </section>
      )}
    </>
  )
}

/* ----------------------------------------------------------- SniffSection */
const SNIFF_KIND_COLOR = {
  session: '#0A84FF', seed: ACCENT_DARK, key: ACCENT_DARK, seedkey: GREEN,
  download: ACCENT_DARK, upload: '#0A84FF', erase: '#D70015', checkmemory: '#0A84FF',
  routine: MUTED, transfer: FAINT, exit: MUTED, reset: MUTED, did: MUTED, nrc: '#D70015',
}

function SniffSection({ events, meta }) {
  const [backend, setBackend] = useState('simulator')
  const [backends, setBackends] = useState([])
  const [baudrate, setBaudrate] = useState(500000)
  const [extended, setExtended] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const feedRef = useRef(null)

  useEffect(() => {
    api.getBackends().then((d) => setBackends(d.backends || [])).catch(() => {})
  }, [])
  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [events])

  const running = !!meta?.running
  const report = meta?.report
  const isJ2534 = String(backend).startsWith('j2534') || ['tactrix', 'openport'].includes(backend)
  const hasJ2534 = backends.some((b) => String(b.id).startsWith('j2534') && b.available)

  const start = async () => {
    setBusy(true); setErr(null)
    try {
      const r = await api.startSniff({ backend, baudrate: Number(baudrate), extended })
      if (!r.started) setErr(r.error || 'Sniffer läuft bereits.')
    } catch (e) { setErr(String(e.message || e)) } finally { setBusy(false) }
  }
  const stop = () => api.stopSniff().catch(() => {})

  return (
    <>
      <section style={cardStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 0' }}>
          <span style={{ fontSize: 16, fontWeight: 700 }}>Sniffer · Fremd-Flash mitschneiden</span>
          <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: running ? ACCENT_DARK : MUTED }}>
            {running && <span style={{ width: 7, height: 7, borderRadius: '50%', background: ACCENT, animation: 'pulse 1.2s ease-in-out infinite' }} />}
            {running ? `Hört mit · ${meta.frames || 0} Frames` : 'Bereit'}
          </span>
        </div>
        <div style={{ padding: '4px 20px 0', fontSize: 12, color: MUTED, lineHeight: 1.5 }}>
          Hört <b>rein passiv</b> mit, während ein anderes Werkzeug (z.&nbsp;B. Autotuner) über
          einen geteilten OBD2-Bus liest/schreibt — <b>sendet selbst nichts</b> — und leitet
          danach Profil&nbsp;+&nbsp;Seed/Key ab. „Simulator" fährt einen Demo-Flash zum Vorführen ohne Hardware.
        </div>
        <div style={{ padding: '16px 20px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr auto', gap: 12, alignItems: 'end' }}>
            <div>
              <label style={fieldLabel}>Interface</label>
              <select style={inputStyle} value={backend} disabled={running}
                onChange={(e) => setBackend(e.target.value)}>
                {backends.map((b) => (
                  <option key={b.id} value={b.id} disabled={!b.available}>
                    {b.name}{b.available ? '' : ' (nicht verfügbar)'}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label style={fieldLabel}>Baudrate</label>
              <input style={inputStyle} type="number" value={baudrate} disabled={running || !isJ2534}
                onChange={(e) => setBaudrate(e.target.value)} />
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: MUTED, paddingBottom: 9 }}>
              <input type="checkbox" checked={extended} disabled={running || !isJ2534}
                onChange={(e) => setExtended(e.target.checked)} />
              29-bit
            </label>
          </div>
          {isJ2534 && !hasJ2534 && (
            <div style={{ fontSize: 11.5, color: MUTED }}>
              Kein J2534-Gerät gefunden. Tactrix-Treiber:{' '}
              <a href={TACTRIX_DRIVER_URL} target="_blank" rel="noreferrer" style={{ color: ACCENT, fontWeight: 600 }}>herunterladen</a>
            </div>
          )}
          {err && <div style={{ fontSize: 12, color: '#D70015' }}>{err}</div>}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button onClick={running ? stop : start} disabled={busy}
              style={{ padding: '10px 22px', borderRadius: 9, fontSize: 14, fontWeight: 700, color: '#fff', background: running ? '#D70015' : ACCENT, boxShadow: running ? 'none' : '0 4px 12px -4px rgba(255,122,0,.5)' }}>
              {running ? 'Stoppen' : 'Sniffer starten'}
            </button>
            <span style={{ marginLeft: 'auto', fontFamily: MONO, fontSize: 11.5, color: FAINT }}>
              {meta?.frames || 0} Frames · {meta?.ids || 0} CAN-IDs
            </span>
          </div>

          <div ref={feedRef} style={{
            maxHeight: 260, overflowY: 'auto', background: '#0B0B0C', borderRadius: 10,
            padding: '10px 12px', fontFamily: MONO, fontSize: 12, lineHeight: 1.65,
          }}>
            {events.length === 0 && (
              <div style={{ color: FAINT }}>
                {running ? 'Warte auf Bus-Verkehr … jetzt am anderen Werkzeug lesen/schreiben starten.'
                  : 'Noch nichts. „Sniffer starten" klicken.'}
              </div>
            )}
            {events.map((e, i) => (
              <div key={i} style={{ color: SNIFF_KIND_COLOR[e.kind] || '#E8E8ED', whiteSpace: 'pre-wrap' }}>
                <span style={{ color: FAINT }}>[{typeof e.t === 'number' ? e.t.toFixed(3) : ''}] </span>{e.text}
              </div>
            ))}
          </div>
        </div>
      </section>

      {meta?.error && (
        <section style={{ ...cardStyle, padding: '14px 18px', color: '#D70015', fontSize: 13 }}>
          Fehler: {meta.error}
        </section>
      )}

      {report && (
        <section style={{ ...cardStyle, padding: '16px 20px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ fontSize: 15, fontWeight: 700 }}>Ergebnis · abgeleitet aus dem Mitschnitt</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 10 }}>
            {[['Frames', report.frames], ['Requests', report.requests], ['Sitzungen', (report.sessions || []).join(', ') || '—'],
              ['Security', (report.securityLevels || []).join(', ') || '—'], ['erase', report.eraseRoutine || '—'],
              ['checkMemory', report.checkMemory || '—']].map(([k, v]) => (
              <div key={k} style={{ background: 'rgba(0,0,0,.04)', borderRadius: 9, padding: '9px 12px' }}>
                <div style={{ fontSize: 11, color: MUTED }}>{k}</div>
                <div style={{ fontFamily: MONO, fontSize: 13, fontWeight: 600 }}>{v}</div>
              </div>
            ))}
          </div>

          <div>
            <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 6 }}>Seed/Key-Paare</div>
            {(report.seedKeyPairs || []).length === 0
              ? <div style={{ fontSize: 12.5, color: MUTED }}>keine erfasst</div>
              : (report.seedKeyPairs || []).map((p, i) => (
                <div key={i} style={{ fontFamily: MONO, fontSize: 13, color: GREEN }}>
                  L{p.level}: seed={p.seed} → key={p.key}
                </div>
              ))}
          </div>

          <div>
            <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 6 }}>Download-Blöcke (Memory-Map)</div>
            {(report.downloadBlocks || []).length === 0
              ? <div style={{ fontSize: 12.5, color: MUTED }}>keine erfasst</div>
              : (report.downloadBlocks || []).map((b, i) => (
                <div key={i} style={{ fontFamily: MONO, fontSize: 13 }}>
                  {b.address} · {b.size} Bytes · {b.transfers} Transfers
                </div>
              ))}
          </div>

          {report.hasFiles && (
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              {[['profile', 'Profil (YAML)'], ['pairs', 'Seed/Key (.txt)'], ['log', 'Mitschnitt (candump)']].map(([kind, label]) => (
                <a key={kind} href={api.sniffDownloadUrl(kind)} download
                  style={{ padding: '9px 16px', borderRadius: 8, fontSize: 13, fontWeight: 600, background: 'rgba(0,0,0,.05)', color: '#1D1D1F', textDecoration: 'none' }}>
                  ↓ {label}
                </a>
              ))}
            </div>
          )}
        </section>
      )}
    </>
  )
}

/* ------------------------------------------------------------ DiagSection */
function DiagSection({ onIdentified }) {
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [addr, setAddr] = useState('0x80008000')
  const [size, setSize] = useState(256)
  const [mem, setMem] = useState(null)
  const [cks, setCks] = useState(null)

  const run = async (fn, set) => {
    setBusy(true); setErr(null)
    try { set(await fn()) } catch (e) { setErr(String(e.message || e)) } finally { setBusy(false) }
  }
  const scan = () => run(() => api.scanEcu(false), (r) => {
    setReport(r)
    if (r.identification?.length) onIdentified(r.identification)
  })
  const readMem = () => run(() => api.readMemory(addr, Number(size)), setMem)
  const checksum = () => run(() => api.getChecksum(), setCks)
  const fixChecksum = () => run(async () => {
    await api.correctChecksum()
    return api.getChecksum()
  }, setCks)

  const row = (a, b, mono) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '7px 0', borderBottom: '1px solid rgba(0,0,0,.05)' }}>
      <span style={{ fontSize: 12.5, color: MUTED }}>{a}</span>
      <span style={{ fontSize: 12.5, fontFamily: mono ? MONO : 'inherit', textAlign: 'right', wordBreak: 'break-all' }}>{b}</span>
    </div>
  )

  return (
    <>
      <section style={cardStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 0' }}>
          <span style={{ fontSize: 16, fontWeight: 700 }}>Diagnose · Steuergerät auslesen</span>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: FAINT }}>nur lesend</span>
        </div>
        <div style={{ padding: '4px 20px 0', fontSize: 12, color: MUTED }}>
          Sitzungen, Identifikation (Teilenummern, VIN) und Security-Access-Level ermitteln. Schreibt nichts ins Steuergerät.
        </div>
        <div style={{ padding: '16px 20px 20px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <button onClick={scan} disabled={busy}
              style={{ padding: '10px 22px', borderRadius: 9, fontSize: 14, fontWeight: 700, color: '#fff', background: busy ? 'rgba(0,0,0,.15)' : ACCENT }}>
              {busy ? 'Läuft …' : 'Steuergerät scannen'}
            </button>
            {report && (
              <span style={{ fontSize: 12.5, color: report.online ? GREEN : '#D70015', fontWeight: 500 }}>
                {report.online ? `Online · ${report.txId}/${report.rxId}` : 'Keine Antwort'}
              </span>
            )}
          </div>
          {err && <div style={{ fontSize: 12, color: '#D70015' }}>{err}</div>}
          {report && (
            <div>
              {row('Sitzungen', report.sessions.join(', ') || '—', true)}
              {row('Programmier-Level', report.programmingLevel || 'nicht gefunden', true)}
              {report.identification.map((d) => row(d.name || d.did, d.value || '—', true))}
              {report.seeds.map((s) => row(`Seed ${s.level}`, s.info, true))}
            </div>
          )}
        </div>
      </section>

      <section style={cardStyle}>
        <div style={{ padding: '16px 20px 0', fontSize: 16, fontWeight: 700 }}>Speicher lesen</div>
        <div style={{ padding: '14px 20px 20px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr auto', gap: 12, alignItems: 'end' }}>
            <div>
              <label style={fieldLabel}>Adresse</label>
              <input style={{ ...inputStyle, fontFamily: MONO }} value={addr}
                onChange={(e) => setAddr(e.target.value)} />
            </div>
            <div>
              <label style={fieldLabel}>Bytes</label>
              <input style={inputStyle} type="number" min="1" max="65536" value={size}
                onChange={(e) => setSize(e.target.value)} />
            </div>
            <button onClick={readMem} disabled={busy}
              style={{ padding: '9px 20px', borderRadius: 8, fontSize: 13.5, fontWeight: 600, background: 'rgba(0,0,0,.06)' }}>Auslesen</button>
          </div>
          {mem && (
            <>
              <div style={{ fontFamily: MONO, fontSize: 11.5, color: MUTED }}>
                {mem.address} · {mem.size} B · CRC32 {mem.crc32}
              </div>
              <div style={{ background: '#F5F5F7', borderRadius: 10, padding: '10px 12px', fontFamily: MONO, fontSize: 11, lineHeight: 1.7, maxHeight: 190, overflow: 'auto', wordBreak: 'break-all' }}>
                {(mem.hex.match(/.{1,32}/g) || []).map((line, i) => (
                  <div key={i}>
                    <span style={{ color: FAINT }}>{(i * 16).toString(16).padStart(4, '0')}  </span>
                    {(line.match(/.{1,2}/g) || []).join(' ')}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </section>

      <section style={cardStyle}>
        <div style={{ padding: '16px 20px 0', fontSize: 16, fontWeight: 700 }}>Prüfsummen (MEDC17)</div>
        <div style={{ padding: '4px 20px 0', fontSize: 12, color: MUTED }}>
          Prüft die interne Flash-Prüfsummen der im Flashen-Tab geladenen Datei und kann sie korrigieren.
        </div>
        <div style={{ padding: '14px 20px 20px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'flex', gap: 10 }}>
            <button onClick={checksum} disabled={busy}
              style={{ padding: '9px 20px', borderRadius: 8, fontSize: 13.5, fontWeight: 600, background: 'rgba(0,0,0,.06)' }}>Prüfen</button>
            {(() => {
              // Only offer a correction when blocks were actually found and at
              // least one of them is wrong.
              const fixable = !!cks && cks.blocks > 0 && cks.allOk === false
              return (
                <button onClick={fixChecksum} disabled={busy || !fixable}
                  style={{ padding: '9px 20px', borderRadius: 8, fontSize: 13.5, fontWeight: 600, color: '#fff', background: fixable ? GREEN : 'rgba(0,0,0,.15)' }}>Korrigieren</button>
              )
            })()}
          </div>
          {cks && (
            <div>
              {row('Datei', cks.name)}
              {row('Regionen', String(cks.blocks))}
              {cks.regions.map((r, i) => row(
                `${r.start}–${r.end} (${r.algorithm})`,
                r.ok ? '✓ ok' : `✗ ${r.computed} ≠ ${r.target}`, true))}
              {cks.blocks === 0 && (
                <div style={{ fontSize: 12, color: MUTED, paddingTop: 8 }}>
                  Keine MEDC17-Prüfsummenblöcke gefunden — bei einer reinen Kalibrierdatei ist das normal.
                </div>
              )}
            </div>
          )}
        </div>
      </section>
    </>
  )
}

/* ------------------------------------------------------------- LogSection */
function LogSection({ logs, logRef, autoscroll, onToggle, onExport }) {
  return (
    <section style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '14px 20px' }}>
        <span style={{ fontSize: 16, fontWeight: 700 }}>Protokoll</span>
        <button onClick={onToggle} style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: MUTED }}>
          Autoscroll
          <Switch on={autoscroll} onColor="#34C759" w={34} />
        </button>
        <button className="link-action" onClick={onExport} disabled={!logs.length}
          style={{ fontSize: 13, fontWeight: 500, color: logs.length ? ACCENT_DARK : FAINT }}>Exportieren</button>
      </div>
      <div style={{ padding: '0 12px 12px' }}>
        <div ref={logRef} style={{ background: '#F5F5F7', borderRadius: 12, fontFamily: MONO, fontSize: 11.5, lineHeight: 1.8, height: 186, overflowY: 'auto', overflowX: 'hidden', padding: '12px 14px', fontVariantNumeric: 'tabular-nums' }}>
          {logs.map((line, i) => (
            <div key={i} style={{ display: 'flex', gap: 12, animation: 'fadeSlide .3s ease both' }}>
              <span style={{ color: FAINT, flexShrink: 0 }}>{line.time}</span>
              <span style={{ color: LOG_COLORS[line.cls] || LOG_COLORS[''], minWidth: 0, flex: 1, overflowWrap: 'break-word' }}>{line.msg}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

/* ---------------------------------------------------------------- Sidebar */
function Sidebar({ vehicle, onTool }) {
  const c = vehicle.connection
  const infoCard = { ...cardStyle, padding: '6px 18px' }
  const row = (label, value, valueStyle) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '9px 0', borderBottom: '1px solid rgba(0,0,0,.05)' }}>
      <span style={{ fontSize: 13 }}>{label}</span>
      <span style={{ fontSize: 13, color: MUTED, ...valueStyle }}>{value}</span>
    </div>
  )
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
      <section style={infoCard}>
        <div style={{ fontSize: 13, fontWeight: 600, color: MUTED, padding: '12px 0 4px' }}>Steuergerät</div>
        {row('Typ', vehicle.ecu)}
        {row('Prozessor', vehicle.processor)}
        {row('SW-Nummer', vehicle.swNumber, { fontFamily: MONO, fontSize: 12 })}
        {row('HW-Nummer', vehicle.hwNumber, { fontFamily: MONO, fontSize: 12 })}
        {row('Flash-Größe', vehicle.flashSize)}
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '9px 0 14px' }}>
          <span style={{ fontSize: 13 }}>Tuning-Schutz</span>
          <span style={{ fontSize: 13, color: ACCENT_DARK, fontWeight: 500 }}>{vehicle.protection}</span>
        </div>
      </section>

      <section style={infoCard}>
        <div style={{ fontSize: 13, fontWeight: 600, color: MUTED, padding: '12px 0 4px' }}>Verbindung</div>
        {row('Modus', c.mode, { color: ACCENT_DARK, fontWeight: 500 })}
        {row('Interface', c.interface)}
        {row('Protokoll', c.protocol)}
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '9px 0 14px' }}>
          <span style={{ fontSize: 13 }}>Session</span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: GREEN, fontWeight: 500 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#34C759' }} />Aktiv
          </span>
        </div>
      </section>

      <section style={infoCard}>
        <div style={{ fontSize: 13, fontWeight: 600, color: MUTED, padding: '12px 0 4px' }}>Werkzeuge</div>
        <ToolRow bg="rgba(255,122,0,.12)" label="Speicher lesen" onClick={() => onTool('diag')}
          icon={<g><circle cx="12" cy="12" r="9" /><path d="M8 12l3 3 5-6" /></g>} stroke={ACCENT_DARK} />
        <ToolRow bg="rgba(52,199,89,.14)" label="Prüfsumme korrigieren" onClick={() => onTool('diag')}
          icon={<g><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" /><path d="M9 12l2 2 4-4" /></g>} stroke={GREEN} />
        <ToolRow last bg="rgba(0,0,0,.06)" label="Firmware laden" onClick={() => onTool('flash')}
          icon={<g><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></g>} stroke="#1D1D1F" />
      </section>

      <div style={{ fontSize: 11, color: FAINT, textAlign: 'center', lineHeight: 1.6 }}>
        MED17 Flasher {vehicle.version ? 'v' + vehicle.version : ''}<br />DME Innovation GmbH
      </div>
    </div>
  )
}

function ToolRow({ bg, label, icon, stroke, last, onClick }) {
  return (
    <button onClick={onClick} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: last ? '9px 0 14px' : '9px 0', borderBottom: last ? 'none' : '1px solid rgba(0,0,0,.05)', width: '100%', textAlign: 'left' }}>
      <span style={{ width: 27, height: 27, borderRadius: 7, background: bg, display: 'grid', placeItems: 'center', flexShrink: 0 }}>
        <svg viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ width: 14, height: 14 }}>{icon}</svg>
      </span>
      <span style={{ fontSize: 13, flex: 1 }}>{label}</span>
      <span style={{ color: '#C7C7CC', fontSize: 15 }}>›</span>
    </button>
  )
}

/* ----------------------------------------------------------------- icons */
function Switch({ on, onColor, w }) {
  const knob = w === 38 ? 19 : 17
  const travel = w === 38 ? 15 : 13
  return (
    <span style={{ width: w, height: w === 38 ? 23 : 21, borderRadius: 99, background: on ? onColor : 'rgba(0,0,0,.12)', position: 'relative', transition: 'background .2s', flexShrink: 0 }}>
      <span style={{ position: 'absolute', top: 2, left: 2, width: knob, height: knob, borderRadius: '50%', background: '#fff', boxShadow: '0 1px 3px rgba(0,0,0,.25)', transform: `translateX(${on ? travel : 0}px)`, transition: 'transform .2s' }} />
    </span>
  )
}
function Check({ w, stroke, sw, top }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" style={{ width: w, height: w, flexShrink: 0, position: 'relative', top: top || 0 }}><path d="M20 6L9 17l-5-5" /></svg>
}
function CheckCircle() {
  return <svg viewBox="0 0 24 24" fill="none" stroke={GREEN} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 14, height: 14, flexShrink: 0 }}><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="9" /></svg>
}
function Warn() {
  return <svg viewBox="0 0 24 24" fill="none" stroke={ACCENT} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ width: 14, height: 14, flexShrink: 0, marginTop: 2 }}><path d="M12 3L2 20h20z" /><path d="M12 10v4" /><path d="M12 17.5v.5" /></svg>
}
