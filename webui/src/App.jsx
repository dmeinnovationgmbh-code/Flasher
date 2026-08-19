import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { LOG_COLORS, fmtAddr, fmtBytes, fmtEta, nowTime } from './format.js'

const ACCENT = '#FF7A00'
const ACCENT_DARK = '#E86E00'
const GREEN = '#1F9D4D'
const MUTED = '#6E6E73'
const FAINT = '#AEAEB2'
const MONO = "ui-monospace,'SF Mono','JetBrains Mono',monospace"

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
        writeLabel={running ? 'Läuft …' : 'Schreiben'} onWrite={() => startFlash(null)} />

      <main style={{
        display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 324px', gap: 18,
        maxWidth: 1180, margin: '0 auto', padding: '0 22px 56px', alignItems: 'start',
      }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
          <WriteSection flash={flash} p={p} badgeText={badgeText} badgeColor={badgeColor}
            running={running} onAbort={abortFlash} file={vehicle.file} />
          <ExpertSection running={running} />
          <MapsSection maps={maps} addons={addons} setAddons={setAddons}
            buying={buying} unlocked={unlocked} onBuy={buy} onFlash={startFlash} />
          <LogSection logs={logs} logRef={logRef} autoscroll={autoscroll}
            onToggle={() => setAutoscroll((a) => !a)} />
        </div>

        <Sidebar vehicle={vehicle} />
      </main>
    </>
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
      <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.04em', whiteSpace: 'nowrap', textTransform: 'uppercase' }}>
        MED17.7.5 Flash Tool
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
function VehicleBar({ vehicle, running, writeLabel, onWrite }) {
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
        <button className="btn-secondary" style={{ padding: '10px 22px', borderRadius: 9, background: '#FFFFFF', border: '1px solid rgba(0,0,0,.12)', fontSize: 14, fontWeight: 600 }}>Lesen</button>
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
          <button disabled style={{ padding: '8px 18px', borderRadius: 8, fontSize: 13, fontWeight: 600, background: 'rgba(0,0,0,.03)', color: FAINT }}>Recovery</button>
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
    seedUrl: '', seedPath: '', seedOptions: '', allowWrite: false,
  })
  const [fw, setFw] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)

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

  const upd = (k, v) => setForm((f) => ({ ...f, [k]: v }))
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

  const seedCfg = () => {
    const s = form.seedSource
    if (s === 'server') return { source: 'server', url: form.seedUrl }
    if (s === 'dll' || s === 'exe') return { source: s, path: form.seedPath, options: form.seedOptions }
    if (s === 'store') return { source: 'store', path: form.seedPath }
    return { source: 'profile' }
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

  const canFlash = form.profileId && fw && !running && !busy && (!isReal || form.allowWrite)
  const seedNeedsUrl = form.seedSource === 'server'
  const seedNeedsPath = ['dll', 'exe', 'store'].includes(form.seedSource)

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
          </div>
        </div>

        <div>
          <label style={fieldLabel}>Firmware-Datei</label>
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
              <option value="dll">Vendor-DLL (J2534)</option>
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

        {msg && (
          <div style={{ fontSize: 12, color: msg.ok ? GREEN : '#D70015' }}>{msg.t}</div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
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

/* ------------------------------------------------------------- LogSection */
function LogSection({ logs, logRef, autoscroll, onToggle }) {
  return (
    <section style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '14px 20px' }}>
        <span style={{ fontSize: 16, fontWeight: 700 }}>Protokoll</span>
        <button onClick={onToggle} style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: MUTED }}>
          Autoscroll
          <Switch on={autoscroll} onColor="#34C759" w={34} />
        </button>
        <button className="link-action" style={{ fontSize: 13, fontWeight: 500, color: ACCENT_DARK }}>Exportieren</button>
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
function Sidebar({ vehicle }) {
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
        <ToolRow bg="rgba(255,122,0,.12)" label="Virtual Read"
          icon={<g><circle cx="12" cy="12" r="9" /><path d="M8 12l3 3 5-6" /></g>} stroke={ACCENT_DARK} />
        <ToolRow bg="rgba(52,199,89,.14)" label="Prüfsumme korrigieren"
          icon={<g><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" /><path d="M9 12l2 2 4-4" /></g>} stroke={GREEN} />
        <ToolRow last bg="rgba(0,0,0,.06)" label="Original-Datei laden"
          icon={<g><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></g>} stroke="#1D1D1F" />
      </section>

      <div style={{ fontSize: 11, color: FAINT, textAlign: 'center', lineHeight: 1.6 }}>
        MED17 Flash Tool v2.4.1 · Build 8812<br />DME Innovation GmbH
      </div>
    </div>
  )
}

function ToolRow({ bg, label, icon, stroke, last }) {
  return (
    <button style={{ display: 'flex', alignItems: 'center', gap: 11, padding: last ? '9px 0 14px' : '9px 0', borderBottom: last ? 'none' : '1px solid rgba(0,0,0,.05)', width: '100%', textAlign: 'left' }}>
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
