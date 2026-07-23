import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import {
  addDirective, approve, bootstrap, createProject, createSession, fetchRoutes, probeSkills,
  runSandbox, searchEvidence, startWorkflow, taskLineage, voiceSocketUrl,
  archiveConversation, branchTurn, createConversation, appendTurn, deleteConversation, fetchChatRoutes,
  getConversation, listConversations, renameConversation, retryTurn, searchConversations,
  sendMessage, unarchiveConversation,
  fetchPrdContent, fetchPrdTasks, fetchTaskEligibility,
  transcribeVoice, speakMessage, fetchPreferences, fetchVoiceCatalog,
  fetchAdvancedRoutes, runAdvancedBranch, runAdvancedDispatch, ADVANCED_NOT_CONFIGURED,
  fetchAdvancedPresets, resolveAdvancedPreset, buildAdvancedComparison,
  fetchAdvancedTemplates, resolveAdvancedTemplate, renderAdvancedDeclaredInstructions,
  fetchRatingCriteria, recordAdvancedRating, fetchRatingAggregates,
  ADVANCED_PLAYGROUND_NOT_CONFIGURED,
} from './api'
import {
  describeConversation, selectChatRoute, successorTurnBody, terminalToStatus,
  initialVoiceInputState, voiceInputReducer, voiceInputLabel, voiceDraftReady,
  initialPlaybackState, playbackReducer, playbackLabel, isPlaybackActiveFor, shouldAutoplay,
  voiceAutoplayFromPreferences,
  routeProvenanceLabel, isRouteDiverged, divergenceAnnouncement,
} from './chat-api'
import { submittedControls, isBranchSettled, comparisonAttemptStatus } from './advanced-chat'
import SettingsView from './settings-view'
import ConfigurationView from './configuration-view'
import ModelHealthIndicator from './model-health-indicator'
import PluginCatalogView from './plugin-catalog-view'
import AdvancedPanel from './advanced-chat-view'
import AdvancedPlaygroundPanel from './advanced-playground-view'
import {
  deliverBlockReason, describeEligibility, describePrdContent, describeTaskReference,
  filterDescribedTasks, freshnessLabel, nextDeliverCandidate, progressSummaryLabel,
  workflowEntryModel,
} from './delivery-explorer'

const emptyData = {
  projects: [], runs: [], sessions: [], workflows: [], approvals: [], skills: [], directives: [], audit: [],
  router_configured: false, sandbox: { available: false, models: [] },
  voice: { available: false, transport: 'not_configured', retains_transcripts: false },
}

// Chat is first and selected by default; Delivery stays reachable directly below
// it (chat-first-voice T004.4). The order here IS the rendered nav order.
const nav = [
  ['Chat', '◇'], ['Voice', '⏺'], ['Delivery', '⌘'], ['Explorer', '▦'], ['Sessions', '◫'], ['Runs', '↗'], ['Routes', '⌁'], ['Approvals', '✓'], ['Settings', '⚙'],
  ['Evidence', '◈'], ['Skills', '✦'], ['Plugins', '⧉'], ['Sandbox', '□'],
]

function Mark() { return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span> }
function Status({ tone = 'green', children }) { return <span className={`status status-${tone}`}><span className="dot" />{children}</span> }
function tone(status) { return ['reconciliation', 'pending', 'not_configured', 'unavailable'].includes(status) ? 'amber' : 'green' }
// A run is a machine record (run id + State task id); its HUMAN name is the
// title of the session it belongs to, joined here from the same server-served
// bootstrap payload the Sessions/Delivery views already correlate against — it
// is never client-fabricated. The requested route (run.model) prefixes it as
// the work-class intent. When a run has no session (e.g. a bare POST /api/runs),
// compose the label from the real fields that DO exist (route + task id) so the
// primary line is still a human sentence, never a bare id.
function titleCase(value) { return value ? value.charAt(0).toUpperCase() + value.slice(1) : value }
function runTitle(run, session) { const intent = titleCase(run.model) || 'Delivery'; if (session?.title) return `${intent} · ${session.title}`; if (run.task_id) return `${intent} run for task ${run.task_id}`; return `${intent} run` }

function Rail({ active, setActive, onNewDelivery, onProfile }) {
  return <aside className="rail">
    <div className="brand"><Mark /><span>Anvil<br /><em>Workbench</em></span></div>
    <nav>{nav.map(([label, glyph]) => <button key={label} aria-label={label} aria-current={active === label ? 'page' : undefined} className={active === label ? 'nav-item selected' : 'nav-item'} onClick={() => setActive(label)}><b aria-hidden="true">{glyph}</b>{label}</button>)}</nav>
    <div className="rail-footer"><button className="new-run" onClick={onNewDelivery}><span aria-hidden="true">+</span> New delivery</button><button className="profile" aria-label="Operator menu" onClick={onProfile}><span>AW</span><div><strong>Operator</strong><small>tailnet owner</small></div><b aria-hidden="true">···</b></button></div>
  </aside>
}

// The Anvil Voice Realtime relay contract is 16 kHz PCM16 mono in BOTH
// directions. Upstream `voice/messages.py` pins the input `sample_rate: int =
// 16000`, and the operator manifest resamples the TTS output to
// `target_sample_rate = 16000`. The playback AudioContext AND the capture
// AudioContext MUST both construct at this exact rate: a mismatch is what
// garbled the live test — STT interpreted 24k audio as 16k (0.67x, slurred
// transcripts) and TTS 16k audio played at 24k (1.5x chipmunk). One named
// constant is used at every rate-coupled site (both AudioContexts and the
// playback createBuffer) so the two directions can never silently drift apart.
const VOICE_RELAY_SAMPLE_RATE = 16000

function encodePcm16(samples) {
  const bytes = new Uint8Array(samples.length * 2)
  const view = new DataView(bytes.buffer)
  samples.forEach((sample, index) => view.setInt16(index * 2, Math.max(-1, Math.min(1, sample)) * 0x7fff, true))
  let binary = ''
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
  return window.btoa(binary)
}

// --- Voice tab session tuning (voice control bar additions) ------------------
//
// The Voice tab lets the operator steer HOW the assistant responds (a live system
// prompt) and WHICH voice it speaks in, both applied on the fly via the realtime
// relay's session.update. The relay bounds+scrubs `instructions` and already
// allows `voice`, so this is a communication-style control, never a privilege.
// The last values persist locally so a reload keeps the operator's setup.
const VOICE_INSTRUCTIONS_KEY = 'workbench.voice.instructions'
const VOICE_VOICE_KEY = 'workbench.voice.voice'
const VOICE_PRESET_KEY = 'workbench.voice.preset'
// Mirror the relay ceiling (workbench/voice.py MAX_INSTRUCTIONS_CHARS) so the
// field clamps before the relay would ever fail closed on an oversize prompt.
const VOICE_MAX_INSTRUCTIONS = 6000

// Curated personality presets. Each is a STARTING POINT: selecting one loads its
// prompt into the still-editable Instructions field and applies it; any edit
// after that flips the selection to a "Custom" state. Client-side constant — no
// new endpoint, and the same bound + relay scrub apply to the loaded text.
const VOICE_PRESETS = [
  { id: 'default', label: 'Default', instructions: 'You are a helpful, natural-sounding voice assistant. Answer clearly and directly.' },
  { id: 'concise', label: 'Concise', instructions: 'Answer in as few words as possible. Prefer one or two short sentences. No preamble, no filler.' },
  { id: 'friendly', label: 'Friendly', instructions: 'Speak warmly and encouragingly, like a supportive colleague. Keep it upbeat and human, but stay useful.' },
  { id: 'professional', label: 'Professional', instructions: 'Respond formally and precisely, in a measured, businesslike tone. Avoid slang and hedging.' },
  { id: 'socratic', label: 'Socratic', instructions: 'Guide the person to the answer by asking one focused clarifying question at a time before offering a direct conclusion.' },
  { id: 'storyteller', label: 'Storyteller', instructions: 'Answer with a light, playful, storytelling flair. Use vivid, concrete images, but still get to the point.' },
]

// Best-effort read/write of a small persisted voice setting. A private-mode or
// unavailable localStorage never breaks the tab — it just does not persist.
function loadVoicePref(key, fallback = '') {
  try { const value = window.localStorage?.getItem(key); return value == null ? fallback : value }
  catch { return fallback }
}
function saveVoicePref(key, value) {
  try { window.localStorage?.setItem(key, value) } catch { /* persistence is best-effort */ }
}

// Kokoro-style voice ids encode accent + gender as a two-letter prefix
// (`af_bella` = US female, `bm_george` = British male). Prettify to a friendly
// label for the picker while the raw id is still what gets sent upstream.
const VOICE_ACCENTS = { a: 'US', b: 'British', e: 'Spanish', f: 'French', h: 'Hindi', i: 'Italian', j: 'Japanese', p: 'Portuguese', z: 'Chinese' }
const VOICE_GENDERS = { f: 'female', m: 'male' }
function prettifyVoice(voice) {
  const id = voice.id || ''
  // Prefer a distinct human label; many serves (e.g. Kokoro) set name === id, in
  // which case prettify the id itself rather than echo the raw token.
  if (voice.name && voice.name !== id) return voice.name
  const underscore = id.indexOf('_')
  const prefix = underscore > 0 ? id.slice(0, underscore) : ''
  const rest = underscore > 0 ? id.slice(underscore + 1) : id
  const name = rest ? rest.charAt(0).toUpperCase() + rest.slice(1) : id
  const accent = VOICE_ACCENTS[prefix.charAt(0)]
  const gender = VOICE_GENDERS[prefix.charAt(1)]
  const trait = [accent, gender].filter(Boolean).join(', ')
  return trait ? `${name} (${trait})` : name
}

// The chat DICTATION (STT) and READ-ALOUD (TTS) request/response lanes speak a
// WAV/PCM16 contract, distinct from the realtime relay above. Dictation captures
// 16 kHz mono PCM16 and wraps it in a minimal WAV header (the Dark STT serve
// accepts a real WAV and there is no server-side transcoder). Read-aloud receives
// raw PCM16 at the serve's OWN sample rate (kokoro's 24 kHz, reported per response)
// and must play it back at THAT rate — a hardcoded rate is the same class of
// garble the realtime fix addressed. These helpers are the single rate-honest
// bridge between raw PCM16 and a browser-decodable WAV.
function _bytesToBase64(bytes) {
  let binary = ''
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
  return window.btoa(binary)
}
function _base64ToBytes(base64) {
  const binary = window.atob(base64 || '')
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}
// Wrap raw little-endian PCM16 mono bytes in a 44-byte WAV header for `sampleRate`.
function _wrapPcm16Wav(pcmBytes, sampleRate) {
  const buffer = new Uint8Array(44 + pcmBytes.length)
  const view = new DataView(buffer.buffer)
  const ascii = (offset, text) => { for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index)) }
  ascii(0, 'RIFF'); view.setUint32(4, 36 + pcmBytes.length, true); ascii(8, 'WAVE')
  ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  ascii(36, 'data'); view.setUint32(40, pcmBytes.length, true)
  buffer.set(pcmBytes, 44)
  return buffer
}
// Capture direction: Float32 samples -> base64 WAV (PCM16 at `sampleRate`).
function float32ToWavBase64(samples, sampleRate) {
  const pcm = new Uint8Array(samples.length * 2)
  const view = new DataView(pcm.buffer)
  for (let index = 0; index < samples.length; index += 1) view.setInt16(index * 2, Math.max(-1, Math.min(1, samples[index])) * 0x7fff, true)
  return _bytesToBase64(_wrapPcm16Wav(pcm, sampleRate))
}
// Playback direction: base64 raw PCM16 -> a `data:audio/wav` URI honoring the
// serve-reported `sampleRate`, so the existing HTMLAudioElement controls play it.
function pcm16Base64ToWavDataUri(pcmBase64, sampleRate) {
  return `data:audio/wav;base64,${_bytesToBase64(_wrapPcm16Wav(_base64ToBytes(pcmBase64), sampleRate))}`
}

// The text stored for an assistant turn whose reply was audio-only. Raw audio is
// NEVER persisted — only this honest text placeholder marks that a spoken reply
// happened, so a saved (or cross-modality) conversation reads truthfully.
const VOICE_SPOKEN_PLACEHOLDER = '[Spoken reply — the assistant replied by voice; audio was not recorded.]'

// Adapt stored conversation turns (content blocks) into the flat voice-transcript
// shape. Loaded turns are historical: they carry no live audio buffer, so they
// render without a Replay control (`live` is absent).
function voiceTurnsFromStored(turns) {
  return (Array.isArray(turns) ? turns : []).map((turn, index) => ({
    id: turn.id || `stored-${index}`,
    role: turn.role === 'assistant' ? 'assistant' : 'user',
    text: (turn.content || []).map((block) => block.text || '').join(''),
    status: 'complete',
  }))
}

// --- Shared conversation rail (unified store) --------------------------------
//
// Voice conversations ARE Chat conversations. This hook owns the conversation
// list/search/select/create/manage cycle over the SAME `/api/conversations`
// store the Chat surface uses (api.js create/list/search/get/append/rename/
// archive/delete) — never a parallel store — so a conversation spoken in Voice
// loads through the exact API Chat reads, and vice versa. It degrades honestly:
// a 503/unconfigured store sets `unavailable` (the caller then runs ephemeral)
// rather than throwing. `onOpen(id, turns)` fires ONLY on an explicit
// select/new/delete so the caller can load/clear its transcript; an
// auto-created conversation (mid-utterance) never fires it, so live turns
// already on screen are preserved and simply re-scoped to the new conversation.
function useConversationRail({ append, enabled = true, onOpen }) {
  const [conversations, setConversations] = useState([])
  const [includeArchived, setIncludeArchived] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [renamingId, setRenamingId] = useState(null)
  const [confirmingDeleteId, setConfirmingDeleteId] = useState(null)
  const [unavailable, setUnavailable] = useState(false)
  const listSeqRef = useRef(0)
  const listAbortRef = useRef(null)
  const railRef = useRef(null)
  // The active-target id readable from an async settle without a stale closure:
  // a voice turn persisting after a switch reads THIS to record into the current
  // conversation, and a select/delete updates it synchronously.
  const selectedIdRef = useRef(null)
  // One in-flight auto-create is shared so two quick turns cannot create two
  // conversations (the first utterance's user + assistant turns race otherwise).
  const creatingRef = useRef(null)

  const refreshList = async (nextQuery, nextArchived, signal) => {
    const seq = (listSeqRef.current += 1)
    try {
      const value = nextQuery && nextQuery.trim()
        ? await searchConversations(nextQuery.trim(), { includeArchived: nextArchived, signal })
        : await listConversations({ includeArchived: nextArchived, signal })
      if (seq !== listSeqRef.current) return // a newer list/search superseded this one
      setConversations(value.conversations || [])
      setUnavailable(false)
    } catch (error) {
      if (signal?.aborted || error?.name === 'AbortError') return // superseded fetch aborted
      // Degrade honestly: mark the rail unavailable (the caller runs ephemeral).
      // No toast — an unconfigured store is an expected, non-error mode here.
      if (seq === listSeqRef.current) setUnavailable(true)
    }
  }
  // Latest-wins list/search with debounce + abort, mirroring the Chat rail.
  useEffect(() => {
    if (!enabled) return undefined
    const controller = new AbortController()
    listAbortRef.current?.abort()
    listAbortRef.current = controller
    if (!query.trim()) { refreshList(query, includeArchived, controller.signal); return () => controller.abort() }
    const handle = setTimeout(() => { refreshList(query, includeArchived, controller.signal) }, 150)
    return () => { clearTimeout(handle); controller.abort() }
  }, [query, includeArchived, enabled])

  const focusRail = () => {
    const rail = railRef.current
    if (!rail) return
    const target = rail.querySelector('.conv-open') || rail.querySelector('.conv-new')
    target?.focus()
  }

  const select = async (id) => {
    selectedIdRef.current = id
    setSelectedId(id); setRenamingId(null); setConfirmingDeleteId(null)
    if (!id) { onOpen?.(null, []); return }
    try { const value = await getConversation(id); if (selectedIdRef.current === id) onOpen?.(id, value.turns || []) }
    catch { if (selectedIdRef.current === id) { onOpen?.(id, []); append('That conversation could not be opened.') } }
  }
  const newConversation = async () => {
    try {
      const record = await createConversation({})
      setConversations((current) => [record, ...current])
      await select(record.id)
      return record.id
    } catch { append('A new conversation could not be created.'); return null }
  }
  // Ensure a durable target for a turn about to persist. If one is already
  // selected, reuse it; otherwise auto-create ONE (shared across a racing pair)
  // and select it WITHOUT clearing the live transcript (no onOpen).
  const ensureConversation = (title) => {
    if (selectedIdRef.current) return Promise.resolve(selectedIdRef.current)
    if (creatingRef.current) return creatingRef.current
    creatingRef.current = (async () => {
      try {
        const record = await createConversation(title ? { title } : {})
        setConversations((current) => [record, ...current])
        selectedIdRef.current = record.id; setSelectedId(record.id)
        return record.id
      } catch { return null }
      finally { creatingRef.current = null }
    })()
    return creatingRef.current
  }
  const rename = async (id, title) => {
    setRenamingId(null)
    try { const record = await renameConversation(id, title); setConversations((current) => current.map((item) => (item.id === id ? record : item))) }
    catch { append('The conversation could not be renamed.') }
  }
  const archive = async (id) => { try { await archiveConversation(id); await refreshList(query, includeArchived); focusRail() } catch { append('The conversation could not be archived.') } }
  const unarchive = async (id) => { try { await unarchiveConversation(id); await refreshList(query, includeArchived); focusRail() } catch { append('The conversation could not be unarchived.') } }
  const remove = async (id) => {
    setConfirmingDeleteId(null)
    try {
      await deleteConversation(id)
      if (selectedIdRef.current === id) { selectedIdRef.current = null; setSelectedId(null); onOpen?.(null, []) }
      await refreshList(query, includeArchived); focusRail()
    } catch { append('The conversation could not be deleted.') }
  }

  return {
    conversations, selectedId, selectedIdRef, includeArchived, query, renamingId, confirmingDeleteId, railRef, unavailable,
    select, newConversation, ensureConversation,
    setQuery, toggleArchived: () => setIncludeArchived((value) => !value),
    rename, archive, unarchive, remove,
    startRename: (id) => { setConfirmingDeleteId(null); setRenamingId(id) },
    cancelRename: () => setRenamingId(null),
    requestDelete: (id) => { setRenamingId(null); setConfirmingDeleteId(id) },
    cancelDelete: () => setConfirmingDeleteId(null),
  }
}

// --- Voice: a dedicated speech-to-speech page (its OWN top-level tab) ----------
//
// The ONE realtime relay surface, promoted from a panel bolted onto Delivery into
// its own voice-first page: a session-bound `voice/realtime` websocket that
// streams mic audio up and plays synthesized audio back. It is a conversational
// modality, not a delivery action — session-bound and relay-only, and NO delivery
// action can be started from voice.
//
// A voice conversation IS a conversation: the page carries the SAME rail as Chat
// (create/select/switch/rename/archive/delete over `/api/conversations`), and as
// each turn COMPLETES it is appended to the selected conversation AS TEXT — a
// completed user turn as its transcribed text, a completed assistant turn as an
// audio-only spoken-reply placeholder. RAW AUDIO IS NEVER PERSISTED. So a Voice
// conversation reopens through the exact API Chat reads, and is continuable
// across modalities (spoken here, typed in Chat). If no conversation is selected
// when speaking begins, one is auto-created so nothing is lost. Switching
// conversations re-scopes which conversation new turns persist to and loads that
// conversation's prior turns into the transcript view; the realtime socket is
// session-bound and stays open across the switch. When the conversation store is
// unavailable (503/unconfigured) the rail degrades honestly and voice runs
// ephemeral (live transcript only, cleared on disconnect) rather than erroring.
//
// Two capture modes, both operator-switchable:
//   * Hold to talk — press-hold streams `input_audio_buffer.append`; on release
//     send `input_audio_buffer.commit` (never response.create). The relay's server
//     is voice-activity-driven: committing the buffer is what makes it transcribe
//     and respond; a `response.create` races ahead of the already-consumed buffer
//     and errors ("no pending input"). Verified live: commit-only → one clean
//     response, 0 errors.
//   * Hands-free (open mic) — continuously stream `append` while listening and
//     close each utterance with a CLIENT-driven `commit`. EMPIRICAL NOTE: the live
//     realtime server does NOT emit `input_audio_buffer.speech_stopped`
//     autonomously from the append stream — it emits speech_started/speech_stopped
//     only as a byproduct of a client commit (append-only, even with real speech +
//     trailing silence, yields zero events and zero responses). So a design that
//     waits for the server's speech_stopped before committing would deadlock. The
//     end-of-utterance boundary is therefore detected CLIENT-side (energy VAD) and
//     drives the commit; a server-sent speech_stopped, if one ever arrives, ALSO
//     triggers the commit. Never response.create. Verified live (real kokoro
//     speech, resampled to 16 kHz): one clean response per utterance, correct
//     transcript, 0 errors, across multiple utterances on one persistent socket.
function VoicePage({ data, append }) {
  const session = data.sessions?.[0]
  const configured = Boolean(data.voice?.available && session)
  const routeLabel = data.voice?.route || 'realtime · fast route'
  const [connection, setConnection] = useState('disconnected') // disconnected | connecting | ready
  const [captureMode, setCaptureMode] = useState('hold') // hold | handsfree
  const [capturing, setCapturing] = useState(false) // mic hot (either mode)
  const [speaking, setSpeaking] = useState(false) // assistant audio playing
  const [transcript, setTranscript] = useState([]) // {id, role, text, status, live}
  const [announce, setAnnounce] = useState('')
  // Session tuning: the live system prompt (draft + last-applied) and the chosen
  // TTS voice, both persisted and re-sent on (re)connect. `preset` names the
  // curated personality that seeded the draft, or 'custom' once the operator
  // edits it. `voices` is the picker's options, fetched from the hub.
  // Clamp on LOAD, not only on Apply: a corrupted/oversize stored value must
  // degrade to the bound rather than be sent verbatim on connect and trip the
  // relay's fail-closed bound (which would drop the whole session).
  const [instructionsDraft, setInstructionsDraft] = useState(() => loadVoicePref(VOICE_INSTRUCTIONS_KEY).slice(0, VOICE_MAX_INSTRUCTIONS))
  const [instructionsApplied, setInstructionsApplied] = useState(() => loadVoicePref(VOICE_INSTRUCTIONS_KEY).slice(0, VOICE_MAX_INSTRUCTIONS))
  const [preset, setPreset] = useState(() => loadVoicePref(VOICE_PRESET_KEY, 'custom') || 'custom')
  const [voice, setVoice] = useState(() => loadVoicePref(VOICE_VOICE_KEY))
  const [voices, setVoices] = useState([])

  const socketRef = useRef(null)
  // Refs mirror the applied instructions and voice so the async socket.onopen
  // (and any later Apply) always reads the CURRENT value, not a stale closure.
  const instructionsRef = useRef(instructionsApplied)
  const voiceRef = useRef(voice)
  const captureRef = useRef(null)
  // A SINGLE persistent playback context with a running playhead. A streamed
  // response arrives as many `response.output_audio.delta` chunks; a fresh
  // AudioContext per chunk (a) starts every chunk at t=0 so they overlap and
  // (b) exhausts the browser's ~6-context cap mid-response, which threw and cut
  // playback off. Decode each chunk into the ONE context and schedule it at
  // `nextStart` so the chunks play back gaplessly in order.
  const playbackRef = useRef(null)
  const pendingSourcesRef = useRef(0)
  // Buffered assistant audio, keyed by assistant turn id, so each "spoken reply"
  // bubble can Replay its own turn's audio. This is transient in-memory only —
  // never persisted anywhere.
  const chunkBufRef = useRef(new Map())
  // The assistant's spoken WORDS, keyed by assistant turn id — accumulated from the
  // `response.output_audio_transcript.delta` stream (#281) so the bubble shows the
  // real reply as it streams AND the persisted turn keeps the actual text (never the
  // placeholder) when a transcript arrived. Text only; audio is never persisted.
  const assistantTextRef = useRef(new Map())
  const currentAssistantRef = useRef(null)
  const currentUserRef = useRef(null)
  const turnSeqRef = useRef(0)
  const mountedRef = useRef(true)
  // Client-side end-of-utterance VAD state for hands-free (the server does not
  // stream autonomous VAD — see the note above).
  const heardSpeechRef = useRef(false)
  const silenceStartRef = useRef(0)
  const utterancePendingRef = useRef(false)
  // Serializes turn persistence so appended turns keep transcript order even when
  // a user turn and the assistant's reply settle back-to-back on one utterance.
  const persistQueueRef = useRef(Promise.resolve())
  // The OWNER of the in-flight exchange: the conversation a spoken utterance (and
  // the reply it triggers) belongs to, captured when the utterance STARTS. Both
  // the user turn and its assistant reply bind to this, so a mid-utterance switch
  // can never split an exchange across two conversations or misattribute a
  // straggler transcription to the newly-selected thread. `{ dest, id }`: `dest`
  // is the bound destination promise (funnelled through the shared auto-create),
  // `id` pins the concrete conversation id once known (incl. an auto-created one).
  const utteranceRef = useRef(null)
  // True while a user utterance is in flight (its STT has started but not
  // completed). On a switch this decides whether the owner is a live straggler to
  // preserve (keep, so its completed still routes to the origin) or a finished
  // exchange's stale owner to drop (clear, so the next utterance re-binds).
  const userUtteranceOpenRef = useRef(false)

  // The unified conversation rail — the SAME `/api/conversations` store as Chat.
  // `enabled` gates loading on a configured relay; `onOpen` loads a selected
  // conversation's turns into (or clears) the transcript. `loadConversationInto
  // Transcript` is a hoisted declaration below, so it is available here.
  const rail = useConversationRail({ append, enabled: configured, onOpen: loadConversationIntoTranscript })
  // Mirror availability into a ref so the socket.onclose closure (captured at
  // connect) reports the CURRENT persistence mode, not a stale one.
  const railUnavailableRef = useRef(false)
  railUnavailableRef.current = rail.unavailable
  const disconnectMessage = () => (railUnavailableRef.current
    ? 'Disconnected. Nothing was recorded — chat storage is unavailable, so this session was ephemeral.'
    : 'Disconnected. The live view cleared; saved conversations remain in your list.')

  // Resolve the destination conversation NOW (at utterance/settle time), funnelled
  // through the shared creatingRef auto-create so a first utterance's user and
  // assistant turns share ONE new conversation. Unavailable store → no destination.
  const resolveDestination = () => (rail.unavailable
    ? Promise.resolve(null)
    : rail.ensureConversation(`Voice · ${new Date().toLocaleString()}`))

  // Bind the current exchange to its owner at utterance START (the first STT
  // event). `id` snapshots the origin (or null while an auto-create is pending)
  // and is pinned to the concrete id once the destination resolves, so the render
  // guard can compare it against the live selection.
  const beginUtterance = () => {
    const dest = resolveDestination()
    const owner = { dest, id: rail.selectedIdRef.current }
    dest.then((resolved) => { if (resolved) owner.id = resolved }).catch(() => {})
    utteranceRef.current = owner
    userUtteranceOpenRef.current = true
    return owner
  }
  const startUserUtterance = () => utteranceRef.current || beginUtterance()
  // Does the in-flight utterance belong to the CURRENTLY VIEWED conversation? A
  // straggler STT event that arrives after a switch is NOT owned by the new view:
  // it must route to its origin and never render into the switched-to thread.
  // While an auto-create is still pending (id unpinned) the origin is the current
  // view only if nothing else has been selected yet.
  const ownedByView = (owner) => {
    if (!owner) return true
    if (owner.id == null) return rail.selectedIdRef.current == null
    return rail.selectedIdRef.current === owner.id
  }

  // Append ONE completed turn AS TEXT (never audio) to a destination CAPTURED at
  // settle time (`dest`) — never re-read the live selection inside the drained
  // task, so a switch mid-flight cannot misattribute the turn. No-op when the
  // store is unavailable (voice runs ephemeral). Best-effort: a failed append is
  // swallowed so the live session is never blocked.
  const persistVoiceTurn = (role, text, dest) => {
    if (rail.unavailable) return
    const target = dest || resolveDestination()
    persistQueueRef.current = persistQueueRef.current.then(async () => {
      const id = await target
      if (!id) return
      try { await appendTurn(id, { role, status: 'complete', content: [{ kind: 'text', text }] }) }
      catch { /* best-effort; a persistence failure never blocks speaking */ }
    })
  }

  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; teardownAll(); socketRef.current?.close?.(); socketRef.current = null } }, [])

  // Populate the voice picker from the hub (never the TTS serve directly). A
  // missing/unconfigured catalog degrades quietly to the serve's default voice.
  useEffect(() => {
    if (!configured) return undefined
    let alive = true
    fetchVoiceCatalog()
      .then((list) => { if (alive) setVoices(Array.isArray(list) ? list : []) })
      .catch(() => { if (alive) setVoices([]) })
    return () => { alive = false }
  }, [configured])

  const closePlayback = () => {
    const pb = playbackRef.current
    if (!pb) return
    pb.context.close?.().catch?.(() => undefined); playbackRef.current = null; pendingSourcesRef.current = 0
  }
  const releaseCapture = () => {
    const capture = captureRef.current
    if (!capture) return
    try { capture.processor.disconnect() } catch { /* already gone */ }
    try { capture.source.disconnect() } catch { /* already gone */ }
    capture.stream?.getTracks?.().forEach((track) => { try { track.stop() } catch { /* already stopped */ } })
    try { capture.context.close?.() } catch { /* already closing */ }
    captureRef.current = null
  }
  const teardownAll = () => { releaseCapture(); closePlayback(); if (mountedRef.current) { setCapturing(false); setSpeaking(false) } }

  const ensurePlayback = () => {
    // Playback direction of the 16 kHz relay contract: one shared context at the
    // SAME rate the capture and relay use. A rate mismatch garbles playback.
    if (!playbackRef.current) playbackRef.current = { context: new window.AudioContext({ sampleRate: VOICE_RELAY_SAMPLE_RATE }), nextStart: 0 }
    return playbackRef.current
  }
  const decodeChunk = (context, encoded) => {
    const binary = window.atob(encoded)
    const buffer = context.createBuffer(1, binary.length / 2, VOICE_RELAY_SAMPLE_RATE); const channel = buffer.getChannelData(0)
    const bytes = new Uint8Array(binary.length); for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
    const view = new DataView(bytes.buffer); for (let index = 0; index < channel.length; index += 1) channel[index] = view.getInt16(index * 2, true) / 0x8000
    return buffer
  }
  const scheduleBuffer = (context, buffer) => {
    const pb = playbackRef.current
    const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination)
    pendingSourcesRef.current += 1
    if (mountedRef.current) setSpeaking(true)
    source.onended = () => { pendingSourcesRef.current = Math.max(0, pendingSourcesRef.current - 1); if (pendingSourcesRef.current === 0 && mountedRef.current) setSpeaking(false) }
    const startAt = Math.max(context.currentTime, pb.nextStart)
    source.start(startAt); pb.nextStart = startAt + buffer.duration
  }
  const playChunk = (encoded) => {
    if (typeof encoded !== 'string' || !encoded || !window.AudioContext) return
    try { const pb = ensurePlayback(); scheduleBuffer(pb.context, decodeChunk(pb.context, encoded)) }
    catch { setAnnounce('Voice output could not be played in this browser.') }
  }
  // Replay one assistant turn's buffered audio through the shared context.
  const replayTurn = (turnId) => {
    const chunks = chunkBufRef.current.get(turnId) || []
    if (!chunks.length || !window.AudioContext) return
    try { const pb = ensurePlayback(); chunks.forEach((chunk) => scheduleBuffer(pb.context, decodeChunk(pb.context, chunk))); setAnnounce('Replaying the assistant reply.') }
    catch { setAnnounce('Voice output could not be replayed in this browser.') }
  }

  // Barge-in: halt local playback IMMEDIATELY (close + drop the scheduled queue so
  // nextStart resets on the next chunk) AND tell the server to stop generating
  // (`response.cancel` is in the relay allowlist; never response.create). Gating on
  // the SYNCHRONOUS playbackRef makes it idempotent — once the assistant audio is
  // torn down, a second trigger in the same instant sends no duplicate cancel, so
  // an auto barge-in and a manual Stop can never double-cancel. Returns whether it
  // actually interrupted a playing assistant.
  const bargeIn = (reason) => {
    if (!playbackRef.current && pendingSourcesRef.current === 0) return false
    closePlayback(); if (mountedRef.current) setSpeaking(false)
    if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'response.cancel' }))
    if (reason) setAnnounce(reason)
    return true
  }
  // Manual Stop always halts and cancels, even if playback already drained — the
  // operator asked for silence, so honor it unconditionally.
  const bargeInStop = () => {
    if (bargeIn('Stopped the assistant. You can speak again.')) return
    closePlayback(); if (mountedRef.current) setSpeaking(false)
    if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'response.cancel' }))
    setAnnounce('Stopped the assistant. You can speak again.')
  }

  const upsertUserTurn = (text, final) => {
    if (currentUserRef.current) {
      const id = currentUserRef.current
      setTranscript((current) => current.map((turn) => (turn.id === id ? { ...turn, text, status: final ? 'complete' : 'partial' } : turn)))
      if (final) currentUserRef.current = null
    } else {
      const id = `voice-user-${(turnSeqRef.current += 1)}`
      if (!final) currentUserRef.current = id
      setTranscript((current) => [...current, { id, role: 'user', text, status: final ? 'complete' : 'partial' }])
    }
    setAnnounce(`You said: ${text}`)
  }
  const beginAssistantTurn = () => {
    const id = `voice-assistant-${(turnSeqRef.current += 1)}`
    currentAssistantRef.current = id
    chunkBufRef.current.set(id, [])
    assistantTextRef.current.set(id, '')
    // `live` marks a turn whose audio is buffered this session — only those offer
    // Replay. Text stays empty until the assistant-transcript stream (#281) fills
    // it; until then the render falls back to the spoken-reply note. A loaded/
    // historical assistant turn instead shows its stored text/placeholder.
    setTranscript((current) => [...current, { id, role: 'assistant', text: '', status: 'streaming', live: true }])
    setAnnounce('Assistant is replying by voice.')
  }
  // Stream the assistant's spoken WORDS into the CURRENT assistant bubble as they
  // arrive (#281 `response.output_audio_transcript.delta`), correlated by the same
  // current-turn id the audio playback uses. Audio and Replay are unchanged.
  const appendAssistantTranscript = (delta) => {
    if (typeof delta !== 'string' || !delta) return
    if (!currentAssistantRef.current) beginAssistantTurn()
    const id = currentAssistantRef.current
    const next = (assistantTextRef.current.get(id) || '') + delta
    assistantTextRef.current.set(id, next)
    setTranscript((current) => current.map((turn) => (turn.id === id ? { ...turn, text: next } : turn)))
  }
  // Finalize the assistant transcript on `.done`: when the event carries the full
  // transcript, adopt it as authoritative (covers any missed delta); otherwise keep
  // the accumulated deltas. Completion of the turn itself is driven by response.done.
  const finalizeAssistantTranscript = (finalText) => {
    const id = currentAssistantRef.current
    if (!id || typeof finalText !== 'string' || !finalText) return
    assistantTextRef.current.set(id, finalText)
    setTranscript((current) => current.map((turn) => (turn.id === id ? { ...turn, text: finalText } : turn)))
  }
  // A spoken reply has settled: mark its bubble complete and persist the turn AS
  // TEXT (never audio) to the SAME conversation the triggering utterance was bound
  // to — captured at settle time, so a switch cannot split the exchange. Persist the
  // ACTUAL transcript when one arrived (#281); fall back to the audio-only
  // placeholder only when no words were received. Then close the exchange.
  const completeAssistantTurn = () => {
    const id = currentAssistantRef.current
    if (!id) return
    setTranscript((current) => current.map((turn) => (turn.id === id ? { ...turn, status: 'complete' } : turn)))
    const spoken = (assistantTextRef.current.get(id) || '').trim()
    currentAssistantRef.current = null
    persistVoiceTurn('assistant', spoken || VOICE_SPOKEN_PLACEHOLDER, utteranceRef.current?.dest)
    utteranceRef.current = null
  }
  // Load a selected conversation's stored turns into the transcript (or clear it
  // for a fresh/removed one). Settle any in-flight playback from the PREVIOUS
  // conversation and drop its live per-turn refs so a straggling event cannot
  // attach to the newly selected conversation; the realtime socket stays open.
  function loadConversationIntoTranscript(id, turns) {
    if (playbackRef.current || pendingSourcesRef.current > 0) {
      closePlayback(); if (mountedRef.current) setSpeaking(false)
      if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'response.cancel' }))
    }
    currentAssistantRef.current = null; currentUserRef.current = null; chunkBufRef.current = new Map(); assistantTextRef.current = new Map()
    // Keep the owner ONLY while a user utterance is still open — its completed STT
    // is a straggler that must still route to the origin conversation. A finished
    // exchange's owner is stale here and is dropped so the next utterance in the
    // switched-to conversation re-binds instead of inheriting the old destination.
    if (!userUtteranceOpenRef.current) utteranceRef.current = null
    setTranscript(voiceTurnsFromStored(turns))
    setAnnounce(id ? 'Switched conversation. New turns save here.' : 'Started a new conversation.')
  }

  const handleMessage = (event) => {
    let message
    try { message = JSON.parse(event.data) } catch { append('Voice relay returned an unreadable event; no delivery action was started.'); return }
    const type = message.type
    if (type === 'response.output_audio.delta') {
      if (!currentAssistantRef.current) beginAssistantTurn()
      const bucket = chunkBufRef.current.get(currentAssistantRef.current)
      if (bucket && typeof message.delta === 'string') bucket.push(message.delta)
      playChunk(message.delta)
      return
    }
    if (type === 'response.created') { beginAssistantTurn(); return }
    // The assistant's spoken words (#281): stream them into the current bubble, and
    // finalize on `.done`. These arrive only when the session requests audio+text
    // (this page does). Audio playback and Replay are unaffected.
    if (type === 'response.output_audio_transcript.delta') { appendAssistantTranscript(message.delta); return }
    if (type === 'response.output_audio_transcript.done') { finalizeAssistantTranscript(message.transcript); return }
    if (type === 'response.done' || type === 'response.completed') { completeAssistantTurn(); return }
    if (type === 'conversation.item.input_audio_transcription.delta') {
      // Capture the utterance's owner at its first STT event; render the partial
      // ONLY into the view it belongs to (a straggler for a switched-away
      // conversation neither renders here nor starts a bubble in the new thread).
      const owner = startUserUtterance()
      if (ownedByView(owner)) upsertUserTurn(message.delta || message.transcript || '', false)
      return
    }
    if (type === 'conversation.item.input_audio_transcription.completed') {
      const owner = startUserUtterance()
      const text = message.transcript || message.text || ''
      if (ownedByView(owner)) {
        upsertUserTurn(text, true) // owned by the current view: render the final turn
      } else {
        // Straggler after a switch: do NOT render into the new thread. Drop the
        // dangling partial and close this exchange so the next utterance re-binds.
        currentUserRef.current = null
        utteranceRef.current = null
      }
      userUtteranceOpenRef.current = false // user side settled; owner survives for the reply
      // Persist to the destination BOUND at utterance start — the origin
      // conversation — never the live selection at drain time.
      if (text.trim()) persistVoiceTurn('user', text.trim(), owner.dest)
      return
    }
    // The server emits speech_stopped only as a byproduct of a commit, but honor
    // it: if one ever arrives while hands-free, close the utterance with a commit
    // (never response.create). The primary hands-free driver is the client VAD.
    if (type === 'input_audio_buffer.speech_stopped') { if (captureRef.current?.mode === 'handsfree') sendCommit(true); return }
    if (type === 'error') { append('Voice relay rejected an event; no delivery action was started.'); return }
  }

  const connect = () => {
    if (!configured) return
    setConnection('connecting'); setAnnounce('Connecting to the voice relay.')
    const socket = new WebSocket(voiceSocketUrl(session.id)); socketRef.current = socket
    socket.onopen = () => {
      // Apply the operator's session tuning on connect (and again on every
      // reconnect): the chosen voice and the live system prompt. The relay bounds
      // and scrubs `instructions` and already allows `voice`.
      const sessionConfig = { modalities: ['audio', 'text'] }
      if (voiceRef.current) sessionConfig.voice = voiceRef.current
      if (instructionsRef.current) sessionConfig.instructions = instructionsRef.current
      socket.send(JSON.stringify({ type: 'session.update', session: sessionConfig }))
      setConnection('ready'); setAnnounce('Connected and ready. Choose hold-to-talk or hands-free to speak.')
      if (captureMode === 'handsfree') openMic('handsfree')
    }
    socket.onmessage = handleMessage
    socket.onclose = () => { teardownAll(); currentAssistantRef.current = null; currentUserRef.current = null; chunkBufRef.current = new Map(); assistantTextRef.current = new Map(); setTranscript([]); setConnection('disconnected'); setAnnounce(disconnectMessage()) }
    socket.onerror = () => setAnnounce('Voice relay is unavailable. The session and workflow remain unchanged.')
  }
  const disconnect = () => {
    teardownAll(); socketRef.current?.close(); socketRef.current = null
    currentAssistantRef.current = null; currentUserRef.current = null; chunkBufRef.current = new Map()
    setTranscript([]); setConnection('disconnected'); setAnnounce(disconnectMessage())
  }

  // Capture direction of the SAME 16 kHz relay contract: the mic AudioContext MUST
  // match VOICE_RELAY_SAMPLE_RATE so the relay's STT reads real-time, not slowed,
  // audio. `mode` decides how an utterance is CLOSED (hold: on release; handsfree:
  // on client-detected end-of-speech).
  const openMic = async (mode) => {
    if (socketRef.current?.readyState !== WebSocket.OPEN) return
    if (captureRef.current) return // already open
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) { append('This browser cannot capture microphone audio for the Workbench voice relay.'); return }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const context = new window.AudioContext({ sampleRate: VOICE_RELAY_SAMPLE_RATE })
      const source = context.createMediaStreamSource(stream); const processor = context.createScriptProcessor(4096, 1, 1)
      heardSpeechRef.current = false; silenceStartRef.current = 0; utterancePendingRef.current = mode === 'handsfree'
      processor.onaudioprocess = (event) => {
        const frame = event.inputBuffer.getChannelData(0)
        if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: encodePcm16(frame) }))
        if (mode === 'handsfree') runClientVad(frame)
      }
      source.connect(processor); processor.connect(context.destination)
      captureRef.current = { stream, context, source, processor, mode }
      setCapturing(true); setAnnounce(mode === 'handsfree' ? 'Hands-free listening. Speak, then pause to send.' : 'Listening. Release to send.')
    } catch { append('Microphone access was not granted. No audio left this browser.') }
  }
  // Energy-based end-of-utterance detection for hands-free: mark speech when the
  // frame's RMS crosses a threshold, and close the utterance with a commit once a
  // short trailing silence follows detected speech.
  const runClientVad = (frame) => {
    let sum = 0
    for (let index = 0; index < frame.length; index += 1) sum += frame[index] * frame[index]
    const rms = Math.sqrt(sum / (frame.length || 1))
    if (rms > 0.012) {
      // Speech ONSET (silence -> speech) while the assistant is playing is an
      // automatic barge-in: the operator can just talk over it and be heard.
      if (!heardSpeechRef.current) bargeIn('You started talking; the assistant stopped.')
      heardSpeechRef.current = true; utterancePendingRef.current = true; silenceStartRef.current = 0; return
    }
    if (!heardSpeechRef.current) return
    const now = Date.now()
    if (silenceStartRef.current === 0) { silenceStartRef.current = now; return }
    if (now - silenceStartRef.current >= 650) { sendCommit(false); heardSpeechRef.current = false; silenceStartRef.current = 0 }
  }
  // Close an utterance: send commit ONLY, never response.create. `force` (a server
  // speech_stopped) commits regardless of the client pending flag; the client-VAD
  // path only commits when an utterance is actually pending, so an all-silence
  // stretch never commits an empty buffer.
  const sendCommit = (force) => {
    if (socketRef.current?.readyState !== WebSocket.OPEN) return
    if (!force && !utterancePendingRef.current) return
    socketRef.current.send(JSON.stringify({ type: 'input_audio_buffer.commit' }))
    utterancePendingRef.current = false
  }

  // Pressing the talk control while the assistant is playing is an automatic
  // barge-in too: interrupt, then open the mic for the new utterance.
  const startHold = () => { if (connection !== 'ready') return; bargeIn('You pressed to talk; the assistant stopped.'); openMic('hold') }
  const finishHold = () => {
    if (captureRef.current?.mode !== 'hold') return
    releaseCapture(); setCapturing(false)
    if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'input_audio_buffer.commit' }))
    setAnnounce('Sent. Waiting for the assistant.')
  }
  const toggleHandsFree = () => { if (captureRef.current) { releaseCapture(); setCapturing(false); setAnnounce('Hands-free paused.') } else openMic('handsfree') }
  const chooseMode = (mode) => {
    if (mode === captureMode) return
    setCaptureMode(mode)
    // Switching away from an open mic stops it cleanly; switching into hands-free
    // while connected opens the mic immediately.
    if (mode === 'hold' && captureRef.current?.mode === 'handsfree') { releaseCapture(); setCapturing(false) }
    if (mode === 'handsfree' && connection === 'ready' && !captureRef.current) openMic('handsfree')
    setAnnounce(mode === 'hold' ? 'Hold-to-talk mode.' : 'Hands-free mode.')
  }

  // Push the live system prompt to the session (bounded to the relay ceiling) and
  // persist it. When the socket is open it applies immediately; otherwise it is
  // sent on the next connect. Clamps to VOICE_MAX_INSTRUCTIONS so the relay never
  // has to fail closed on an oversize prompt.
  const applySessionInstructions = (text) => {
    const value = (text ?? '').slice(0, VOICE_MAX_INSTRUCTIONS)
    setInstructionsApplied(value); instructionsRef.current = value; saveVoicePref(VOICE_INSTRUCTIONS_KEY, value)
    if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'session.update', session: { instructions: value } }))
  }
  const applyInstructions = () => {
    applySessionInstructions(instructionsDraft)
    setAnnounce('Updated how the assistant should respond.')
  }
  // Editing the field after a preset makes the selection "Custom" — a preset is
  // only ever a starting point.
  const editInstructions = (text) => {
    setInstructionsDraft(text)
    if (preset !== 'custom') { setPreset('custom'); saveVoicePref(VOICE_PRESET_KEY, 'custom') }
  }
  // Selecting a preset loads its prompt into the still-editable field AND applies
  // it, so a pick takes effect at once but stays a starting point.
  const choosePreset = (id) => {
    const found = VOICE_PRESETS.find((item) => item.id === id)
    if (!found) return
    setPreset(id); saveVoicePref(VOICE_PRESET_KEY, id)
    setInstructionsDraft(found.instructions)
    applySessionInstructions(found.instructions)
    setAnnounce(`Personality set to ${found.label}.`)
  }
  const changeVoice = (id) => {
    setVoice(id); voiceRef.current = id; saveVoicePref(VOICE_VOICE_KEY, id)
    // Only a concrete id is sent upstream; the empty "Default voice" choice just
    // leaves the serve's own default in place.
    if (id && socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'session.update', session: { voice: id } }))
    setAnnounce(id ? `Voice set to ${prettifyVoice({ id })}.` : 'Using the default voice.')
  }

  const connected = connection === 'ready'
  const activeConv = rail.conversations.find((record) => record.id === rail.selectedId) || null
  const activeTitle = activeConv ? describeConversation(activeConv).title : null
  // Context-aware empty state: honest about whether turns are being saved.
  const emptyState = rail.unavailable
    ? { head: 'Live, not saved', body: 'Chat storage is unavailable, so this session is ephemeral and clears on disconnect. Raw audio is never recorded.' }
    : rail.selectedId
      ? { head: 'Saved as text', body: 'Speak — your words and each spoken reply are added to this conversation as text you can reopen. Raw audio is never recorded.' }
      : { head: 'Speak to begin', body: 'A conversation is created and saved automatically the moment you talk. Text only — raw audio is never recorded.' }
  const statusLabel = connection === 'disconnected' ? 'Disconnected'
    : connection === 'connecting' ? 'Connecting'
    : speaking ? 'Assistant speaking'
    : capturing ? 'Listening'
    : 'Connected · ready'
  const orbState = connection === 'disconnected' || connection === 'connecting' ? 'idle' : speaking ? 'speaking' : capturing ? 'listening' : 'ready'

  return <div className={`voice-console ${configured ? '' : 'no-rail'}`}>
    {configured && (rail.unavailable
      ? <nav className="conv-rail voice-conv-rail" aria-label="Conversations">
          <div className="conv-rail-head"><p className="conv-rail-title">Conversations</p></div>
          <p className="conv-empty">Chat storage is unavailable, so voice runs without saving. This session is ephemeral and clears on disconnect.</p>
        </nav>
      : <ConversationRail
          railClassName="conv-rail voice-conv-rail"
          caption="Spoken here, saved as text — the same conversations as Chat."
          conversations={rail.conversations} selectedId={rail.selectedId} includeArchived={rail.includeArchived} query={rail.query}
          renamingId={rail.renamingId} confirmingDeleteId={rail.confirmingDeleteId} railRef={rail.railRef}
          onSelect={rail.select} onNew={rail.newConversation} onQueryChange={rail.setQuery} onToggleArchived={rail.toggleArchived}
          onRename={rail.rename} onArchive={rail.archive} onUnarchive={rail.unarchive} onDelete={rail.remove}
          onStartRename={rail.startRename} onCancelRename={rail.cancelRename} onRequestDelete={rail.requestDelete} onCancelDelete={rail.cancelDelete} />)}
    <main className="voice-page" aria-labelledby="voice-heading">
    <header className="voice-head">
      <div className="voice-head-titles">
        <span className="crumb">Voice / speech-to-speech</span>
        <h1 id="voice-heading">Voice</h1>
        <p className="voice-privacy">Session-bound and relay-only. The text transcript is saved as a conversation — the same conversations as Chat, switchable in the list. Raw audio stays on the tailnet and is never recorded or persisted. Voice cannot start a delivery action.</p>
      </div>
      <div className="voice-head-meta">
        <Status tone={connected ? 'green' : 'amber'}>{statusLabel}</Status>
        <dl className="voice-meta-grid">
          <div><dt>Route</dt><dd>{routeLabel}<span className="voice-ro">read-only</span></dd></div>
          <div><dt>Session</dt><dd>{configured ? <>{session.title} <code>{session.id}</code></> : 'not configured'}</dd></div>
        </dl>
      </div>
    </header>

    {!configured
      ? <section className="voice-unconfigured" aria-label="Voice not configured">
          <div className={`voice-orb orb-idle`} aria-hidden="true"><i /><i /><i /></div>
          <h2>Voice is not configured</h2>
          <p>Configure a private Anvil Voice Realtime endpoint and an active session to enable session-bound speech-to-speech. No audio leaves this browser until then.</p>
          <button disabled aria-label="Voice not configured">Voice unavailable</button>
        </section>
      : <>
          <section className="voice-stage" aria-label="Live voice transcript">
            <div className={`voice-orb orb-${orbState}`} aria-hidden="true"><i /><i /><i /></div>
            <div className="voice-thread-bar">
              <span className="voice-thread-label">{activeTitle || 'New conversation'}</span>
              {!rail.unavailable && <span className="voice-thread-badge">{rail.selectedId ? 'saved · text only' : 'auto-saves as text'}</span>}
            </div>
            <ol className="voice-transcript" role="log" aria-live="polite" aria-label="Live transcript">
              {transcript.length === 0
                ? <li className="voice-transcript-empty">
                    <b>{emptyState.head}</b>
                    <span>{emptyState.body}</span>
                  </li>
                : transcript.map((turn) => <li key={turn.id} className={`voice-turn voice-turn-${turn.role} ${turn.status === 'partial' ? 'is-partial' : ''}`}>
                    <span className="voice-turn-role">{turn.role === 'user' ? 'You' : 'Assistant · spoken reply'}</span>
                    <p className="voice-turn-text">{turn.role === 'assistant' ? (turn.text?.trim() ? turn.text : 'The assistant replied by voice.') : (turn.text || (turn.status === 'partial' ? '…' : ''))}</p>
                    {turn.role === 'assistant' && turn.live && <button className="voice-replay" aria-label="Replay this spoken reply" onClick={() => replayTurn(turn.id)}>Replay</button>}
                  </li>)}
            </ol>
          </section>

          <div className="voice-controls">
            <div className="voice-mode" role="group" aria-label="Capture mode">
              <span className="voice-mode-label">Mode</span>
              <button type="button" className={`voice-mode-btn ${captureMode === 'hold' ? 'on' : ''}`} aria-label="Hold-to-talk mode" aria-pressed={captureMode === 'hold'} onClick={() => chooseMode('hold')}>Hold to talk</button>
              <button type="button" className={`voice-mode-btn ${captureMode === 'handsfree' ? 'on' : ''}`} aria-label="Hands-free mode" aria-pressed={captureMode === 'handsfree'} onClick={() => chooseMode('handsfree')}>Hands-free</button>
            </div>

            <section className="voice-tune" aria-label="Assistant voice and personality">
              <div className="voice-tune-presets" role="group" aria-label="Personality preset">
                <span className="voice-tune-eyebrow">Personality</span>
                {VOICE_PRESETS.map((item) => (
                  <button key={item.id} type="button" className={`voice-preset-chip ${preset === item.id ? 'on' : ''}`} aria-pressed={preset === item.id} onClick={() => choosePreset(item.id)}>{item.label}</button>
                ))}
                {preset === 'custom' && <span className="voice-preset-chip is-custom" aria-hidden="true">Custom</span>}
              </div>

              <form className="voice-instructions" onSubmit={(event) => { event.preventDefault(); applyInstructions() }}>
                <label htmlFor="voice-instructions-input" className="voice-tune-eyebrow">How should it respond?</label>
                <div className="voice-instructions-row">
                  <textarea
                    id="voice-instructions-input"
                    className="voice-instructions-input"
                    aria-label="Assistant instructions"
                    rows={2}
                    maxLength={VOICE_MAX_INSTRUCTIONS}
                    value={instructionsDraft}
                    onChange={(event) => editInstructions(event.target.value)}
                    placeholder="e.g. Answer briefly, in a warm tone."
                  />
                  <button type="submit" className="voice-instructions-apply" aria-label="Apply instructions" disabled={instructionsDraft === instructionsApplied}>
                    {instructionsDraft === instructionsApplied ? 'Applied' : 'Apply'}
                  </button>
                </div>
              </form>

              <label className="voice-voice-field">
                <span className="voice-tune-eyebrow">Voice</span>
                <select className="voice-voice-select" aria-label="Assistant voice" value={voice} onChange={(event) => changeVoice(event.target.value)}>
                  <option value="">Default voice</option>
                  {voices.map((item) => <option key={item.id} value={item.id}>{prettifyVoice(item)}</option>)}
                </select>
              </label>
            </section>

            <div className="voice-actions">
              {connection === 'disconnected' || connection === 'connecting'
                ? <button className="voice-connect" onClick={connect} disabled={connection === 'connecting'}>{connection === 'connecting' ? 'Connecting…' : 'Connect'}</button>
                : <>
                    {captureMode === 'hold'
                      ? <button
                          type="button"
                          className={`vp-ptt ${capturing ? 'is-live' : ''}`}
                          aria-label="Hold to talk"
                          aria-pressed={capturing}
                          onPointerDown={startHold} onPointerUp={finishHold} onPointerCancel={finishHold} onPointerLeave={finishHold}
                          onKeyDown={(event) => { if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) { event.preventDefault(); startHold() } }}
                          onKeyUp={(event) => { if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); finishHold() } }}
                        >{capturing ? 'Listening… release to send' : 'Hold to talk'}</button>
                      : <button type="button" className={`voice-handsfree ${capturing ? 'is-live' : ''}`} aria-label={capturing ? 'Pause hands-free listening' : 'Start hands-free listening'} aria-pressed={capturing} onClick={toggleHandsFree}>{capturing ? 'Listening — pause' : 'Start listening'}</button>}
                    <button type="button" className="voice-stop" aria-label="Stop the assistant" onClick={bargeInStop} disabled={!speaking}>Stop</button>
                    <button type="button" className="voice-disconnect" aria-label="Disconnect voice" onClick={disconnect}>Disconnect</button>
                  </>}
            </div>
          </div>
        </>}

    {/* One polite live region announces connection/turn changes for a screen
        reader. role="status" keeps it off the region tree so it never collides
        with the page region's name. */}
    <div className="voice-live" role="status" aria-live="polite">{announce}</div>
    </main>
  </div>
}

function Delivery({ data, append, onDirective, onGuide, onDeliverNext }) {
  const project = data.projects[0]
  const run = data.runs.find((item) => ['queued', 'running'].includes(item.status)) || data.runs[0]
  const session = data.sessions.find((item) => item.id === run?.session_id) || data.sessions[0]
  const messages = (data.directives || []).filter((event) => !session || event.session_id === session.id)
  const [input, setInput] = useState('')
  const submit = async (event) => { event.preventDefault(); if (!session || !input.trim()) return; const text = input.trim(); try { await onDirective(session.id, text); setInput('') } catch { append('Direction was not recorded. No future work packet was changed.') } }
  if (!project) return <main className="delivery empty-delivery"><span className="crumb">Delivery / setup required</span><h1>Start a private delivery</h1><p>Workbench has no synthetic delivery. Create a project, register its local bridge, publish reviewed skills, then create a session.</p><button className="session-action" onClick={onGuide}>Open setup guide</button></main>
  return <main className="delivery"><header className="project-header"><div><span className="crumb">Delivery / {project.name}</span><h1>{run?.task_id ? `Task ${run.task_id}` : 'No active task'}</h1><p>PRD → State plan → local Codex run → evidence → approved PR</p></div><div className="project-header-actions"><button className="session-action" onClick={onDeliverNext}>Deliver next task</button><Status tone={run ? tone(run.status) : 'amber'}>{run?.status || 'ready for session'}</Status></div></header>
    <section className="flow-card"><div className="flow-top"><span className="thread-avatar">A</span><div><strong>Delivery operator</strong><small>{run ? `${run.model} through Anvil Serving` : 'Waiting for a bridge-supervised run'}</small></div><Status tone={run ? tone(run.status) : 'amber'}>{run?.status || 'idle'}</Status></div><ol className="steps"><li className={run ? 'complete' : 'current'}><span>{run ? '✓' : '1'}</span><div><b>{run ? 'State work packet requested' : 'Create a session'}</b><small>{run ? `${run.id} is bound to the bridge and its configured worktree.` : 'A session creates a durable workflow and lease boundary.'}</small></div></li><li className={run?.status === 'evidenced' ? 'complete' : run ? 'current' : ''}><span>{run?.status === 'evidenced' ? '✓' : '2'}</span><div><b>Bridge edits and verifies locally</b><small>Redacted transcripts and State evidence return through the bridge.</small></div></li><li className={run?.status === 'evidenced' ? 'current' : ''}><span>3</span><div><b>Review evidence and authorize a hash-bound action</b><small>GitHub remains local to the bridge and requires approval.</small></div></li></ol></section>
    <section className="conversation" aria-label="Delivery directions">{messages.length ? messages.map((message) => <article className="message human" key={message.id}><div className="message-head"><span>OP</span><b>Recorded direction</b><small>event {message.sequence}</small></div><p>{message.data?.content}</p></article>) : <p className="evidence-empty">No recorded delivery directions for this session yet.</p>}</section>
    <form className="composer" onSubmit={submit}><textarea aria-label="Add direction to this delivery" value={input} disabled={!session} onChange={(event) => setInput(event.target.value)} rows="2" placeholder={session ? 'Add a direction for the next work packet…' : 'Create a session before adding delivery direction…'} /><div><small>Saved into the next bridge work packet; it does not interrupt a running Codex process.</small><button type="submit" disabled={!session || !input.trim()} aria-label="Send delivery direction">Send <span aria-hidden="true">↵</span></button></div></form>
  </main>
}

function Trace({ data, setActive, append, refresh, selectedApprovalId, clearApproval }) {
  const run = data.runs[0]; const approval = data.approvals.find((item) => item.id === selectedApprovalId && item.status === 'pending'); const [expanded, setExpanded] = useState(false); const [busy, setBusy] = useState(false)
  const authorize = async () => { if (!approval) return; setBusy(true); try { await approve(approval.id); clearApproval(); append('Approval recorded. The local bridge may consume this exact action once.'); await refresh() } catch { append('Approval was not recorded. The bridge remains unable to create a PR.') } finally { setBusy(false) } }
  return <aside className="trace"><section className="trace-head"><div><span>Live trace</span><Status tone={data.router_configured ? 'green' : 'amber'}>{data.router_configured ? 'router configured' : 'router unavailable'}</Status></div><button aria-label="Show correlation trace" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '−' : '↗'}</button></section><section className="trace-body"><TraceStep label="Intent" value={run?.task_id || 'No task selected'} detail={run ? `run ${run.id}` : 'Start a session to request a State work packet.'} done={Boolean(run)} /><TraceStep label="Route" value={run?.model || 'No route selected'} detail={data.router_configured ? 'decision lookup is available in Routes' : 'router token or URL is missing'} done={Boolean(run && data.router_configured)} /><TraceStep label="Skills" value={data.skills?.length ? `${data.skills.length} bridge-published` : 'No skills published'} detail="Only selected local skills enter a work packet." done={Boolean(data.skills?.length)} /><TraceStep label="Verify" value={run?.status === 'evidenced' ? 'evidence submitted' : 'independent gate pending'} detail="The bridge submits evidence to State before approval." current={run?.status !== 'evidenced'} />{expanded && <div className="raw-trace"><code>workbench_run_id: {run?.id || 'not assigned'}</code><code>task_id: {run?.task_id || 'not assigned'}</code><code>request_id: written by Serving if the model request succeeds</code></div>}</section><section className="evidence-mini"><header><span>Evidence packet</span><button onClick={() => setActive('Evidence')}>View all</button></header><p className="evidence-empty">{run?.status === 'evidenced' ? 'Use Evidence search to inspect cited, redacted artifacts.' : 'Awaiting independent bridge evidence.'}</p></section><section className="approval-card"><div className="approval-title"><span>Approval</span><Status tone={approval ? 'amber' : 'green'}>{approval ? 'selected' : 'selection required'}</Status></div><h2>{approval ? approval.action_type.replaceAll('_', ' ') : 'Select an approval to review'}</h2>{approval ? <><dl className="approval-binding"><div><dt>Approval id</dt><dd>{approval.id}</dd></div><div><dt>Payload hash</dt><dd>{approval.payload_hash}</dd></div><div><dt>Run</dt><dd>{approval.payload?.run_id || 'not bound'}</dd></div><div><dt>Worktree</dt><dd>{approval.payload?.worktree_id || 'not bound'}</dd></div></dl><pre aria-label="Selected approval payload">{JSON.stringify(approval.payload, null, 2)}</pre></> : <p>Open Approvals and choose one pending action. Workbench never guesses which grant you intended.</p>}<p className="muted">Approval is a one-time release; the bridge checks this exact safe payload and binding before it can change GitHub.</p><button disabled={busy || !approval} onClick={authorize}>{busy ? 'Authorizing…' : 'Authorize selected action'} <span aria-hidden="true">→</span></button></section></aside>
}

function TraceStep({ label, value, detail, done, current }) { return <div className={`trace-step ${done ? 'done' : ''} ${current ? 'current' : ''}`}><i>{done ? '✓' : current ? '●' : '○'}</i><div><small>{label}</small><b>{value}</b><span>{detail}</span></div></div> }

function SessionsView({ data, onNewSession, onStartSession }) { const sessions = data.sessions || []; const workflows = data.workflows || []; return <section className="workspace-view"><span className="crumb">Sessions / durable harness contexts</span><div className="view-heading"><div><h1>Concurrent sessions</h1><p>Each session has its own workflow cursor. Worktree leases prevent concurrent sessions from editing the same configured worktree.</p></div><button className="session-action" onClick={onNewSession} disabled={!data.projects[0]}>New concurrent session</button></div><div className="session-list">{sessions.length ? sessions.map((session) => { const workflow = workflows.find((item) => item.session_id === session.id); const activeRun = data.runs?.some((run) => run.session_id === session.id && ['queued', 'running'].includes(run.status)); return <article key={session.id}><div><b>{session.title}</b><small>{session.worktree_id} · {session.id}</small></div><span>{workflow ? `workflow v${workflow.version} · ${workflow.status}` : 'workflow pending'}</span><Status tone={activeRun ? 'green' : tone(workflow?.status)}>{activeRun ? 'active run' : session.status}</Status><button aria-label={`Start delivery ${session.title}`} disabled={!workflow || activeRun || workflow.status !== 'draft'} onClick={() => onStartSession(session, workflow)}>Start delivery</button></article> }) : <article className="session-empty"><b>No harness session yet</b><span>Create a project and bridge, then create a session.</span></article>}</div></section> }

function RunsView({ data, refresh }) { return <section className="workspace-view"><span className="crumb">Runs / bridge-supervised</span><div className="view-heading"><div><h1>Runs</h1><p>Every row is a durable Workbench run, correlated to a State task and selected model.</p></div><button className="session-action" onClick={refresh}>Refresh runs</button></div><div className="data-list">{data.runs.length ? data.runs.map((run) => { const session = data.sessions?.find((item) => item.id === run.session_id); return <article className="run-row" key={run.id}><div><b>{runTitle(run, session)}</b><small>run {run.id}{run.task_id ? ` · task ${run.task_id}` : ''}</small></div><Status tone={tone(run.status)}>{run.status}</Status></article> }) : <article><b>No delivery runs yet</b><span>Start a session workflow to create a bridge command.</span><Status tone="amber">idle</Status></article>}</div></section> }

function RoutesView({ data, append }) { const [routes, setRoutes] = useState([]); const [loaded, setLoaded] = useState(false); const [busy, setBusy] = useState(false); const refresh = async () => { setBusy(true); try { const value = await fetchRoutes(); setRoutes(value.routes || []); setLoaded(true) } catch { append('Route decisions are unavailable. Check the server-held Anvil Serving URL and token.') } finally { setBusy(false) } }; return <section className="workspace-view"><span className="crumb">Routes / Anvil Serving</span><div className="view-heading"><div><h1>Routes</h1><p>Read-only router decision metadata, filtered to Workbench run correlations.</p></div><button className="session-action" onClick={refresh} disabled={!data.router_configured || busy}>{busy ? 'Refreshing…' : 'Refresh decisions'}</button></div>{!data.router_configured ? <div className="sandbox-note"><b>Routes unavailable</b><span>Configure the hub’s Anvil Serving URL and token. The browser never sees either credential.</span></div> : <div className="data-list">{loaded && !routes.length ? <article><b>No correlated decisions yet</b><span>Run a bridge delivery, then refresh after Serving records the request.</span><Status tone="amber">waiting</Status></article> : routes.map((route, index) => <article key={`${route.request_id || index}`}><b>{route.intent || route.model || route.served_model || route.served_tier || 'selected route'}</b><span>{route.workbench_run_id} · {route.task_id || 'no task id'} · {route.request_id || 'no request id'}</span><Status>{route.status || route.tier || route.served_tier || 'recorded'}</Status></article>)}</div>}</section> }

function ApprovalsView({ data, selectApproval }) { const pending = data.approvals.filter((approval) => approval.status === 'pending'); return <section className="workspace-view"><span className="crumb">Approvals / hash-bound</span><h1>Approvals</h1><p>Approval only releases a matching, one-time bridge command; it does not expose a GitHub credential to this browser.</p><div className="data-list">{pending.length ? pending.map((approval) => <article key={approval.id}><b>{approval.action_type.replaceAll('_', ' ')}</b><span>{approval.id} · {approval.payload_hash}</span><button className="inline-action" aria-label={`Review action ${approval.id}`} onClick={() => selectApproval(approval.id)}>Review action</button></article>) : <article><b>No pending approval</b><span>A bridge must submit evidence before a PR action can be requested.</span><Status tone="amber">waiting</Status></article>}</div></section> }

function EvidenceView({ data, append }) { const [query, setQuery] = useState(''); const [results, setResults] = useState([]); const [lineage, setLineage] = useState(null); const project = data.projects[0]; const search = async (event) => { event.preventDefault(); if (!project || !query.trim()) return; try { const value = await searchEvidence(project.id, query.trim()); setResults(value.results || []); setLineage(null) } catch { append('Evidence search is unavailable. The graph remains read-only and never approves actions.') } }; const loadLineage = async (taskId) => { try { setLineage(await taskLineage(taskId)) } catch { append('Task lineage is unavailable for that task.') } }; return <section className="workspace-view"><span className="crumb">Evidence / redacted projection</span><h1>Evidence</h1><p>Searches the read-optimized evidence projection. Anvil State remains canonical for task acceptance.</p><form className="query-form" onSubmit={search}><input aria-label="Evidence query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search evidence and cited artifacts" disabled={!project} /><button disabled={!project || !query.trim()}>Search evidence</button></form><div className="data-list">{results.map((result, index) => <article key={`${result.citation || index}`}><b>{result.title || result.source_id || 'Evidence artifact'}</b><span>{result.citation || result.summary || 'redacted projection result'}</span><Status>cited</Status></article>)}{!results.length && <article><b>No evidence query yet</b><span>Search only returns redacted, cited projection records.</span><Status tone="amber">ready</Status></article>}</div>{data.runs.filter((run) => run.task_id).slice(0, 4).map((run) => <button className="lineage-button" key={run.id} onClick={() => loadLineage(run.task_id)}>Show lineage for {run.task_id}</button>)}{lineage && <pre className="lineage-result">{JSON.stringify(lineage, null, 2)}</pre>}</section> }

function SkillsView({ data, append, refresh }) { const project = data.projects[0]; const [busy, setBusy] = useState(false); const verify = async () => { if (!project) return; setBusy(true); try { await probeSkills(project.id); append('Bridge skill check queued. The bridge verifies local files and matching digests before it reports evidence.'); await refresh() } catch { append('Skills could not be checked. A project bridge must publish configured skill metadata first.') } finally { setBusy(false) } }; return <section className="workspace-view"><span className="crumb">Skills / bridge-local</span><div className="view-heading"><div><h1>Reviewed skills</h1><p>Skills are discovered from explicit local bridge roots. The hub receives only names, descriptions, and digests; paths and bodies stay local.</p></div><button className="session-action" onClick={verify} disabled={!project || !data.skills.length || busy}>{busy ? 'Queueing check…' : 'Verify bridge skills'}</button></div><div className="data-list">{data.skills.length ? data.skills.map((skill) => <article key={`${skill.bridge_id}:${skill.skill_id}`}><b>{skill.skill_id}</b><span>{skill.description} · {skill.content_sha256.slice(0, 12)}…</span><Status>published</Status></article>) : <article><b>No skills published</b><span>Start the local bridge with one or more explicit <code>--skills-root</code> paths.</span><Status tone="amber">bridge setup</Status></article>}</div></section> }

function SandboxView({ data, append }) { const [model, setModel] = useState(data.sandbox?.models?.[0] || ''); const [input, setInput] = useState(''); const [result, setResult] = useState(null); const [busy, setBusy] = useState(false); useEffect(() => { setModel(data.sandbox?.models?.[0] || '') }, [data.sandbox]); const submit = async (event) => { event.preventDefault(); setBusy(true); try { setResult(await runSandbox({ model, input })) } catch { append('Sandbox request was not accepted by Anvil Serving. No provider fallback was used.') } finally { setBusy(false) } }; return <section className="workspace-view"><span className="crumb">Sandbox / Serving only</span><h1>Model sandbox</h1><p>A bounded, audited Responses request through Anvil Serving. It is separate from bridge delivery and cannot create a PR, merge, or change policy.</p>{!data.sandbox?.available ? <div className="sandbox-note"><b>Sandbox unavailable</b><span>Set an allowlisted <code>WORKBENCH_SANDBOX_MODELS</code> value plus the hub’s Serving URL and token.</span></div> : <form className="sandbox-form" onSubmit={submit}><label>Allowed route<select aria-label="Sandbox model" value={model} onChange={(event) => setModel(event.target.value)}>{data.sandbox.models.map((item) => <option key={item}>{item}</option>)}</select></label><label>Prompt<textarea aria-label="Sandbox prompt" value={input} onChange={(event) => setInput(event.target.value)} rows="5" placeholder="Ask a bounded, non-mutating model question" /></label><button disabled={busy || !input.trim()}>{busy ? 'Routing…' : 'Run through Anvil Serving'}</button></form>}{result && <section className="sandbox-result"><b>{result.model} · {result.status}</b><pre>{result.output_text || 'The routed response contained no output_text.'}</pre></section>}</section> }

function WorkspaceView({ active, data, onNewSession, onStartSession, append, refresh, selectApproval }) { if (active === 'Sessions') return <SessionsView data={data} onNewSession={onNewSession} onStartSession={onStartSession} />; if (active === 'Runs') return <RunsView data={data} refresh={refresh} />; if (active === 'Routes') return <RoutesView data={data} append={append} />; if (active === 'Approvals') return <ApprovalsView data={data} selectApproval={selectApproval} />; if (active === 'Evidence') return <EvidenceView data={data} append={append} />; if (active === 'Skills') return <SkillsView data={data} append={append} refresh={refresh} />; return <SandboxView data={data} append={append} /> }

function Modal({ title, children, onClose }) { return <div className="modal-backdrop" role="presentation"><section className="modal" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button aria-label={`Close ${title}`} onClick={onClose}>×</button></header>{children}</section></div> }

function NewDelivery({ onClose, onCreate }) { const [name, setName] = useState(''); const [stateRoot, setStateRoot] = useState('.anvil'); const [busy, setBusy] = useState(false); const submit = async (event) => { event.preventDefault(); if (!name.trim()) return; setBusy(true); try { await onCreate({ name: name.trim(), state_root: stateRoot.trim() || '.anvil' }) } finally { setBusy(false) } }; return <Modal title="New delivery" onClose={onClose}><p>This creates a Workbench project record only. Register a project-local bridge separately; its secret never enters this browser.</p><form className="modal-form" onSubmit={submit}><label>Project name<input aria-label="Project name" value={name} onChange={(event) => setName(event.target.value)} /></label><label>State root<input aria-label="State root" value={stateRoot} onChange={(event) => setStateRoot(event.target.value)} /></label><div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create project'}</button></div></form></Modal> }

function NewSession({ project, skills, onClose, onCreate }) { const [title, setTitle] = useState(''); const [worktree, setWorktree] = useState('default'); const [selected, setSelected] = useState([]); const [busy, setBusy] = useState(false); const toggle = (skill) => setSelected((current) => current.includes(skill) ? current.filter((item) => item !== skill) : [...current, skill]); const submit = async (event) => { event.preventDefault(); if (!project || !title.trim() || !worktree.trim()) return; setBusy(true); try { await onCreate({ project_id: project.id, title: title.trim(), worktree_id: worktree.trim(), skills: selected }) } finally { setBusy(false) } }; return <Modal title="New concurrent session" onClose={onClose}><p>A session is a durable harness context. Its worktree id must match a local bridge configuration before delivery can start.</p><form className="modal-form" onSubmit={submit}><label>Session title<input aria-label="Session title" value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Configured worktree id<input aria-label="Configured worktree id" value={worktree} onChange={(event) => setWorktree(event.target.value)} /></label>{skills.length ? <fieldset className="skill-selector"><legend>Bridge-published skills for this session</legend>{skills.map((skill) => <label key={skill.skill_id}><input type="checkbox" checked={selected.includes(skill.skill_id)} onChange={() => toggle(skill.skill_id)} />{skill.skill_id}</label>)}</fieldset> : null}<div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !project || !title.trim() || !worktree.trim()}>{busy ? 'Creating…' : 'Create session'}</button></div></form></Modal> }

function StartSession({ session, workflow, onClose, onStart }) { const [taskId, setTaskId] = useState(''); const [model, setModel] = useState('planning'); const [busy, setBusy] = useState(false); const submit = async (event) => { event.preventDefault(); if (!taskId.trim()) return; setBusy(true); try { await onStart(workflow.id, { task_id: taskId.trim(), model: model.trim() || 'planning' }) } finally { setBusy(false) } }; return <Modal title={`Start ${session.title}`} onClose={onClose}><p>This queues the workflow’s pinned agent step through the configured local bridge. State claims the task; the browser never selects a filesystem path.</p><form className="modal-form" onSubmit={submit}><label>State task id<input aria-label="State task id" value={taskId} onChange={(event) => setTaskId(event.target.value)} /></label><label>Requested route<input aria-label="Requested route" value={model} onChange={(event) => setModel(event.target.value)} /></label><div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !taskId.trim()}>{busy ? 'Starting…' : 'Start bridge delivery'}</button></div></form></Modal> }

// --- Deliver controls (plan-task-delivery T006) ------------------------------
//
// A focus-managed setup sheet that turns a ready State task into a started run
// in ONE activation. The candidate and its blocked/eligibility truth come from
// the merged read-only delivery-projection GET surface (fetchPrdTasks →
// {tasks}, fetchTaskEligibility → {eligibility}); the start itself is the REAL
// wired POST /api/workflows/{id}/start (startWorkflow → {workflow, run}) — there
// is no separate Deliver route (workbench/deliver.py is deliberately NOT wired),
// so this speaks only the shapes the hub actually serves.
//
// The sheet previews EXACTLY ONE ranked candidate (State's plan head), never a
// batch and never skipping past a blocked head. Every choice is an approved id
// (a startable session by id, a PRD by id) — never a filesystem path or a raw
// command. A blocked candidate disables Deliver with its reason IN TEXT (not
// colour alone) and an aria-describedby binding, and a live region announces the
// candidate / blocked / delivering / error states.
function DeliverSheet({ project, workflows, sessions, runs, onClose, onDeliver }) {
  const [prdInput, setPrdInput] = useState('')
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('')
  const [candidate, setCandidate] = useState(null)
  const [loadedPrdId, setLoadedPrdId] = useState('')
  const [loadError, setLoadError] = useState(null)
  const [eligibility, setEligibility] = useState({ status: 'idle', value: null, message: null })
  const [announce, setAnnounce] = useState('')
  const [busy, setBusy] = useState(false)
  const headingRef = useRef(null)
  const loadSeq = useRef(0)
  const sheetRef = useRef(null)
  const startAbortRef = useRef(null)
  // Guards a state update after the sheet unmounts: a dismissal during busy
  // tears the sheet down while the start promise may still settle, and neither
  // its success nor its abort must poke React state on the gone component.
  const mountedRef = useRef(true)
  // Set-on-mount as well as clear-on-unmount so a StrictMode dev remount (mount →
  // cleanup → remount) does not leave the ref stuck false, which would otherwise
  // swallow setBusy(false)/announce on a genuine start failure in development.
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Only a draft workflow whose session has no active run is startable — the same
  // gate the Sessions view uses. Options carry the session TITLE (human) with the
  // worktree + id as secondary text; the option VALUE is the approved workflow id
  // (never free text). This is the "approved id/title only" choice (criterion 3).
  const startable = (workflows || [])
    .filter((workflow) => workflow.status === 'draft'
      && !(runs || []).some((run) => run.session_id === workflow.session_id && ['queued', 'running'].includes(run.status)))
    .map((workflow) => ({ workflow, session: (sessions || []).find((item) => item.id === workflow.session_id) }))
  const selectedWorkflow = startable.find((item) => item.workflow.id === selectedWorkflowId)?.workflow || null
  // The real route the hub will pin comes from the workflow's entry agent step
  // (SHOULD #2), not a non-existent `workflow.model`. When the definition does
  // not pin one, fall back to the hub's own default ('planning') and label the
  // displayed route as the default so the text never claims a derived route it
  // did not derive.
  const derivedModel = workflowEntryModel(selectedWorkflow)
  const model = derivedModel || 'planning'

  const blockReason = deliverBlockReason({ candidate, eligibility, hasSession: Boolean(selectedWorkflow) })

  // A single dismissal path (Close, Cancel, Escape). While busy it aborts the
  // in-flight start so a hung bridge POST cannot trap the user (a11y #4), then
  // closes. When idle it just closes.
  const dismiss = () => {
    if (busy) startAbortRef.current?.abort()
    onClose()
  }

  // Focus into the sheet on open and restore focus to the opener on close, so a
  // keyboard user is never dropped to <body> (a11y focus management).
  useEffect(() => {
    const opener = document.activeElement
    headingRef.current?.focus()
    return () => { opener?.focus?.() }
  }, [])
  // Document-level Escape closes the sheet even after focus has moved among its
  // controls (a11y): the visible Close button is the discoverable path, Escape
  // the keyboard one. It dismisses even while busy so a hung Deliver is never a
  // trap (#4). But it honours an inner control that already handled Escape
  // (#5): if the event was defaultPrevented, or it targets an open native
  // <select> (whose own Escape dismisses its listbox), the sheet stays open so
  // Escape does not discard the loaded candidate/eligibility out from under a
  // dropdown dismissal.
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return
      if (event.defaultPrevented) return
      if (event.target && event.target.tagName === 'SELECT') return
      dismiss()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [busy, onClose])

  // Keep Tab focus inside the sheet (a11y #7): with aria-modal the background is
  // occluded, so Tab/Shift+Tab must cycle within the dialog rather than reach an
  // aria-hidden background control. Wrap at the first/last enabled focusable.
  const onTrapKeyDown = (event) => {
    if (event.key !== 'Tab') return
    const root = sheetRef.current
    if (!root) return
    const focusables = Array.from(
      root.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
    ).filter((el) => !el.disabled && el.getAttribute('aria-hidden') !== 'true')
    if (!focusables.length) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  const loadCandidate = async () => {
    const prdId = prdInput.trim()
    if (!prdId) return
    const seq = (loadSeq.current += 1)
    setCandidate(null); setLoadError(null); setLoadedPrdId('')
    setEligibility({ status: 'idle', value: null, message: null })
    setAnnounce(`Loading the ranked candidate for PRD ${prdId}…`)
    let tasks
    try {
      const value = await fetchPrdTasks(project.id, prdId)
      if (seq !== loadSeq.current) return // a newer load superseded this one
      tasks = value.tasks || []
    } catch (error) {
      if (seq !== loadSeq.current) return
      setLoadError(error.message)
      setAnnounce(error.message || `PRD ${prdId} tasks could not be loaded.`)
      return
    }
    const cand = nextDeliverCandidate(tasks)
    setCandidate(cand); setLoadedPrdId(prdId)
    if (!cand) { setAnnounce(`PRD ${prdId} has no ranked task to deliver.`); return }
    setAnnounce(`Ranked candidate: ${cand.title}. Checking delivery eligibility…`)
    setEligibility({ status: 'loading', value: null, message: null })
    try {
      const value = await fetchTaskEligibility(project.id, prdId, cand.taskId)
      if (seq !== loadSeq.current) return
      const verdict = describeEligibility(value.eligibility)
      setEligibility({ status: 'loaded', value: verdict, message: null })
      if (verdict && !verdict.eligible) {
        const primary = verdict.reasons[0]
        setAnnounce(`Delivery blocked: ${primary?.explanation || verdict.state}`)
      } else {
        setAnnounce(`Ranked candidate ready to deliver: ${cand.title}`)
      }
    } catch (error) {
      if (seq !== loadSeq.current) return
      setEligibility({ status: 'error', value: null, message: error.message })
      setAnnounce(error.message || 'Delivery eligibility is unavailable for this candidate.')
    }
  }

  // One activation starts EXACTLY ONE Deliver: the busy guard plus the disabled
  // button mean a second click/Enter while the start is in flight cannot fire a
  // second startWorkflow — the idempotent backend is not asked to dedupe a UI
  // double-submit (criterion 1 / no double-fire).
  const deliver = async () => {
    if (busy || blockReason || !candidate || !selectedWorkflow) return
    const controller = new AbortController()
    startAbortRef.current = controller
    setBusy(true)
    setAnnounce(`Delivering ${candidate.title}…`)
    try {
      await onDeliver(selectedWorkflow.id, { task_id: candidate.taskId, model }, controller.signal)
      // On success the app closes the sheet and routes to the resulting run.
    } catch {
      // A dismissal during busy unmounts the sheet (and may abort this start);
      // do not poke state on the gone component.
      if (!mountedRef.current || controller.signal.aborted) return
      setBusy(false)
      setAnnounce('Delivery could not be started. No run was launched.')
    }
  }

  return <div className="modal-backdrop" role="presentation">
    <section className="modal deliver-sheet" role="dialog" aria-modal="true" aria-labelledby="deliver-sheet-title" ref={sheetRef} onKeyDown={onTrapKeyDown}>
      <header>
        <h2 id="deliver-sheet-title" tabIndex={-1} ref={headingRef}>Deliver next task</h2>
        {/* Close stays enabled while busy so a hung Deliver is never a trap (#4). */}
        <button aria-label="Close Deliver next task" onClick={dismiss}>×</button>
      </header>
      <p>Preview State’s next ranked candidate for one PRD and start it through the local bridge in a single activation. The browser never sends a filesystem path or a raw command.</p>

      <div className="deliver-choices">
        <label>Deliver into session
          {startable.length
            ? <select aria-label="Deliver into session" value={selectedWorkflowId} onChange={(event) => setSelectedWorkflowId(event.target.value)}>
                <option value="">Choose a startable session…</option>
                {startable.map(({ workflow, session }) => <option key={workflow.id} value={workflow.id}>{session ? `${session.title} · ${session.worktree_id}` : workflow.id}</option>)}
              </select>
            : <p className="deliver-muted">No startable session. Create a session with a configured worktree and start it here — no path or command is entered in this browser.</p>}
        </label>
        <form className="deliver-open" onSubmit={(event) => { event.preventDefault(); loadCandidate() }}>
          <label>PRD id
            <input aria-label="PRD id" value={prdInput} onChange={(event) => setPrdInput(event.target.value)} placeholder="e.g. release-alpha" />
          </label>
          <button className="explorer-open-prd" type="submit" disabled={!prdInput.trim()}>Load ranked candidate</button>
          <small className="deliver-muted">PRD enumeration is not served; open a PRD by its approved id.</small>
        </form>
      </div>

      <section className="deliver-candidate" aria-label="Ranked candidate">
        {loadError
          ? <p className="explorer-degraded">{loadError}</p>
          : candidate
          ? <>
              <div className="deliver-candidate-head">
                <div>
                  <span className="deliver-candidate-eyebrow">Next ranked candidate{loadedPrdId ? ` · ${loadedPrdId}` : ''}</span>
                  <b className="deliver-candidate-title">{candidate.title}</b>
                  <small className="deliver-candidate-scoped">{candidate.scopedId}</small>
                </div>
                <Status tone={tone(candidate.status)}>{candidate.status}</Status>
              </div>
              <p className="deliver-candidate-meta">Delivery: {candidate.latestDeliveryStatus} · {candidate.dependsOn.length} {candidate.dependsOn.length === 1 ? 'dependency' : 'dependencies'} · route {model}{derivedModel ? '' : ' (default)'}</p>
              <DeliverEligibility eligibility={eligibility} />
            </>
          : <p className="deliver-muted">Load a PRD to preview its single next ranked candidate. Exactly one candidate is shown; blocked dependencies are never silently skipped.</p>}
      </section>

      {/* The disabled Deliver always states WHY in bound text — in EVERY disabled
          state, including the pre-load no-session / no-candidate ones, not only
          when a candidate is loaded (#6). */}
      {blockReason && <p id="deliver-block-reason" className="deliver-block-reason">{blockReason.text} <code>{blockReason.code}</code></p>}
      <div className="deliver-actions">
        {/* Cancel stays enabled while busy so a hung Deliver is never a trap (#4). */}
        <button type="button" className="secondary-button" onClick={dismiss}>Cancel</button>
        <button
          type="button"
          className="deliver-start"
          onClick={deliver}
          disabled={busy || Boolean(blockReason)}
          aria-disabled={busy || Boolean(blockReason) ? 'true' : undefined}
          aria-describedby={blockReason ? 'deliver-block-reason' : undefined}
        >{busy ? 'Delivering…' : candidate ? `Deliver ${candidate.title}` : 'Deliver'} <span aria-hidden="true">→</span></button>
      </div>
      <div className="deliver-live" role="status" aria-live="polite" aria-label="Deliver status">{announce}</div>
    </section>
  </div>
}

function DeliverEligibility({ eligibility }) {
  if (!eligibility || eligibility.status === 'idle') return <p className="deliver-muted">Delivery eligibility has not been checked yet.</p>
  if (eligibility.status === 'loading') return <p className="deliver-muted">Checking delivery eligibility…</p>
  if (eligibility.status === 'error') return <p className="explorer-degraded">{eligibility.message || 'Delivery eligibility is unavailable for this candidate.'}</p>
  const verdict = eligibility.value
  if (!verdict) return <p className="deliver-muted">No delivery eligibility verdict for this candidate.</p>
  return <div className="deliver-eligibility">
    <Status tone={verdict.eligible ? 'green' : 'amber'}>{verdict.state}</Status>
    <ul>{verdict.reasons.map((reason) => <li key={reason.code}><b>{reason.code}</b> — {reason.explanation}</li>)}</ul>
  </div>
}

function Onboarding({ data, onClose, setActive, onNewDelivery, onNewSession }) { const project = data.projects[0]; const steps = [{ label: 'Create a Workbench project', complete: Boolean(project), action: () => project ? setActive('Delivery') : onNewDelivery() }, { label: 'Register a project-local bridge', complete: Boolean(project?.bridge_id), action: () => setActive('Skills') }, { label: 'Publish and verify bridge skills', complete: Boolean(data.skills?.length), action: () => setActive('Skills') }, { label: 'Create a harness session', complete: Boolean(data.sessions?.length), action: () => onNewSession() }, { label: 'Run a State task through the bridge', complete: Boolean(data.runs?.length), action: () => setActive('Sessions') }, { label: 'Review evidence before an approval', complete: Boolean(data.runs?.some((run) => run.status === 'evidenced')), action: () => setActive('Evidence') }]; const current = steps.find((step) => !step.complete) || steps.at(-1); const go = () => { current.action(); onClose() }; return <Modal title="Workbench setup guide" onClose={onClose}><p>This guide reflects live hub state. It never marks a bridge, skill, run, or evidence step complete on its own.</p><ol className="onboarding-steps">{steps.map((step, index) => <li key={step.label} className={step.complete ? 'done' : ''}><span>{step.complete ? '✓' : index + 1}</span>{step.label}</li>)}</ol><button className="session-action" onClick={go}>{current.complete ? 'Review completed setup' : `Continue: ${current.label}`}</button></Modal> }

function Notifications({ audit, read, onRead }) { return <section className="notifications" aria-label="Notifications"><header><b>Recent hub activity</b><button onClick={onRead}>Mark viewed</button></header>{read ? <p>All current activity is marked viewed in this browser.</p> : audit.length ? audit.slice(0, 4).map((event) => <p key={event.id}><b>{event.kind}</b><br />{event.actor}</p>) : <p>No Workbench audit events yet.</p>}</section> }
function ProfileMenu({ data, onClose }) { return <section className="profile-menu" aria-label="Operator menu"><b>{data.actor || 'Allowlisted operator'}</b><span>Tailnet identity verified by the hub</span><small>Project creation and approvals are server-checked. The browser has no bridge, GitHub, or model credential.</small><button onClick={onClose}>Close menu</button></section> }

// --- Chat surface (chat-first-voice T004.2 / T004.3 / T004.4) ----------------
//
// The default surface. A conversation rail (management + search + active/archived
// distinction), a transcript with distinct empty/streaming/interrupted/error
// states, a multiline composer with documented keyboard submission, incremental
// cancellable streaming, retry/branch as visible successors, an Advanced mode
// that opens within Chat, and an allowlisted route selector.

const LIFECYCLE = {
  streaming: 'Streaming response…',
  complete: 'Response complete',
  cancelled: 'Response cancelled',
  interrupted: 'Response interrupted',
  failed: 'Response failed',
}

function turnText(turn) {
  return (turn.content || []).map((block) => block.text || '').join('')
}

function ConversationRow({ record, selected, onSelect, onRename, onArchive, onUnarchive, onDelete, renaming, onStartRename, onCancelRename, confirmingDelete, onRequestDelete, onCancelDelete }) {
  const info = describeConversation(record)
  const [draft, setDraft] = useState(info.title)
  const renameRef = useRef(null)
  useEffect(() => { setDraft(info.title) }, [renaming, info.title])
  // Move focus into the rename field the moment editing opens, and support
  // Escape-to-cancel so keyboard users are never trapped mid-edit (a11y #6).
  useEffect(() => { if (renaming) renameRef.current?.focus() }, [renaming])
  if (renaming) {
    return <li className={`conv-row ${info.archived ? 'is-archived' : 'is-active'}`}>
      <form className="conv-rename" onSubmit={(event) => { event.preventDefault(); if (draft.trim()) onRename(record.id, draft.trim()) }}>
        <input ref={renameRef} aria-label={`Rename ${info.title}`} value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); onCancelRename() } }} />
        <button type="submit" disabled={!draft.trim()}>Save</button>
        <button type="button" onClick={onCancelRename}>Cancel</button>
      </form>
    </li>
  }
  return <li className={`conv-row ${info.archived ? 'is-archived' : 'is-active'} ${selected ? 'selected' : ''}`}>
    <button className="conv-open" aria-current={selected ? 'true' : undefined} aria-label={`Open ${info.title}`} onClick={() => onSelect(record.id)}>
      <span className="conv-title">{info.title}</span>
      <span className="conv-meta">
        <span className={`conv-state conv-state-${info.state}`}>{info.state}</span>
        {info.ephemeral && <span className="conv-badge">ephemeral</span>}
        {info.tags.map((tag) => <span key={tag} className="conv-tag">{tag}</span>)}
      </span>
      <small className="conv-id">{record.id}</small>
    </button>
    <div className="conv-actions">
      <button aria-label={`Rename ${info.title}`} onClick={() => onStartRename(record.id)}>Rename</button>
      {info.archived
        ? <button aria-label={`Unarchive ${info.title}`} onClick={() => onUnarchive(record.id)}>Unarchive</button>
        : <button aria-label={`Archive ${info.title}`} onClick={() => onArchive(record.id)}>Archive</button>}
      {/* Delete is a two-step confirm so a single stray keypress cannot destroy a
          conversation (a11y #7): the first press arms an explicit Confirm/Keep. */}
      {confirmingDelete
        ? <>
            <button className="conv-danger" aria-label={`Confirm delete ${info.title}`} onClick={() => onDelete(record.id)}>Confirm delete</button>
            <button aria-label={`Keep ${info.title}`} onClick={onCancelDelete}>Keep</button>
          </>
        : <button aria-label={`Delete ${info.title}`} onClick={() => onRequestDelete(record.id)}>Delete</button>}
    </div>
  </li>
}

function ConversationRail({ conversations, selectedId, includeArchived, query, renamingId, confirmingDeleteId, railRef, onSelect, onNew, onQueryChange, onToggleArchived, onRename, onArchive, onUnarchive, onDelete, onStartRename, onCancelRename, onRequestDelete, onCancelDelete, railClassName = 'conv-rail', caption }) {
  const active = conversations.filter((record) => record.status !== 'archived')
  const archived = conversations.filter((record) => record.status === 'archived')
  const rowProps = { selected: false, onSelect, onRename, onArchive, onUnarchive, onDelete, onStartRename, onCancelRename, onRequestDelete, onCancelDelete }
  const row = (record) => <ConversationRow key={record.id} record={record} {...rowProps} selected={selectedId === record.id} renaming={renamingId === record.id} confirmingDelete={confirmingDeleteId === record.id} />
  // Heading order (a11y #11): the rail is first in document order, so its label
  // is a non-heading element. The page <h1> in chat-main is the document's first
  // heading, keeping the rank sequence h1 → … without a rail <h2> preceding it.
  return <nav className={railClassName} aria-label="Conversations" ref={railRef}>
    <div className="conv-rail-head"><p className="conv-rail-title">Conversations</p><button className="conv-new" aria-label="Start a new conversation" onClick={onNew}>+ New</button></div>
    {caption && <p className="conv-rail-caption">{caption}</p>}
    <form className="conv-search" role="search" onSubmit={(event) => event.preventDefault()}>
      <input type="search" aria-label="Search conversations" placeholder="Search titles and messages" value={query} onChange={(event) => onQueryChange(event.target.value)} />
    </form>
    <label className="conv-filter"><input type="checkbox" checked={includeArchived} onChange={onToggleArchived} aria-label="Show archived conversations" /> Show archived</label>
    {conversations.length === 0
      ? <p className="conv-empty">{query.trim() ? 'No conversations match that search.' : 'No conversations yet. Start one to begin.'}</p>
      : <>
          <section aria-label="Active conversations"><p className="conv-section-title">Active</p><ul className="conv-list">{active.length ? active.map(row) : <li className="conv-none">No active conversations.</li>}</ul></section>
          {includeArchived && <section aria-label="Archived conversations"><p className="conv-section-title">Archived</p><ul className="conv-list">{archived.length ? archived.map(row) : <li className="conv-none">No archived conversations.</li>}</ul></section>}
        </>}
  </nav>
}

// Project / PRD / task titles as readable context when the conversation is bound
// to a delivery, with the canonical ids available only as secondary disclosure.
// The merged conversation projection does not yet emit this binding, so the panel
// renders defensively from an optional `context` block and shows a truthful
// unlinked state otherwise.
function DeliveryContext({ context }) {
  const rows = context ? [['Project', context.project], ['PRD', context.prd], ['Task', context.task]].filter(([, value]) => value) : []
  if (!rows.length) return <p className="chat-context none">No linked delivery context.</p>
  return <section className="chat-context" aria-label="Linked delivery context">
    {rows.map(([label, value]) => <div key={label} className="context-row">
      <span className="context-label">{label}</span>
      <b className="context-title">{value.title}</b>
      {value.id && <details className="context-id"><summary aria-label={`${label} id`}>id</summary><code>{value.id}</code></details>}
    </div>)}
  </section>
}

function TurnView({ turn, onRetry, onBranch, streamActive, conversationId, autoplayPreference }) {
  const text = turnText(turn)
  const streaming = turn.status === 'streaming'
  const distinct = !streaming && turn.status !== 'complete'
  const lineage = turn.lineage?.kind && turn.lineage.kind !== 'initial' ? turn.lineage.kind : null
  // A tuned Advanced run is a sibling branch turn; surface its `mode:'advanced'`
  // as a distinct badge alongside the lineage chip so it is distinguishable from
  // an ordinary branch turn (S4).
  const advancedMode = turn.mode === 'advanced'
  // Route-resolution marks (chat-first-voice T010), derived ONLY from the
  // Serving-supplied mark carried on the settled turn: a provenance chip that
  // distinguishes an explicitly-selected route from a preference-defaulted one,
  // and a rerouted marker when Serving served a different route than requested.
  // These SURFACE what Serving reported — the client never picks a route.
  const provenance = routeProvenanceLabel(turn.routeResolution)
  const diverged = isRouteDiverged(turn.routeResolution)
  return <li className={`turn turn-${turn.role} turn-${turn.status}`}>
    <div className="turn-head">
      <span className="turn-role">{turn.role === 'user' ? 'You' : 'Assistant'}</span>
      {lineage && <span className="turn-lineage">{lineage}</span>}
      {advancedMode && <span className="turn-advanced" aria-label="Advanced tuned run">advanced</span>}
      {provenance && <span className={`turn-route-provenance provenance-${turn.routeResolution.provenance}`} title={provenance}>{provenance}</span>}
      {diverged && <span className="turn-route-diverged" title="Serving served a different route than requested">rerouted</span>}
      {streaming && <span className="turn-status streaming">streaming…</span>}
      {distinct && <span className={`turn-status turn-status-${turn.status}`}>{turn.status}</span>}
    </div>
    {/* The text stays available before/during/after any audio playback. */}
    <p className="turn-text">{text || (streaming ? '…' : '')}</p>
    {/* Successor actions are disabled while a stream is in flight (a11y #12): a
        retry/branch cannot be issued against history that is still settling. */}
    {turn.role === 'assistant' && !streaming && <div className="turn-actions">
      <button aria-label="Retry this response" disabled={streamActive} onClick={() => onRetry(turn)}>Retry</button>
      <button aria-label="Branch from this response" disabled={streamActive} onClick={() => onBranch(turn)}>Branch</button>
      {/* Read-aloud is transient playback that never mutates this message. */}
      {text.trim() && conversationId && <ReadAloud conversationId={conversationId} turn={turn} autoplayPreference={autoplayPreference} />}
    </div>}
  </li>
}

function Transcript({ selected, turns, streamingTurn, onRetry, onBranch, conversationId, autoplayPreference }) {
  if (!selected) return <div className="chat-empty" role="region" aria-label="No conversation selected"><h2>Select or start a conversation</h2><p>Your conversations are private to you and stay on the tailnet.</p></div>
  const rendered = streamingTurn ? [...turns, streamingTurn] : turns
  if (rendered.length === 0) return <div className="chat-empty" role="region" aria-label="Empty conversation"><h2>No messages yet</h2><p>Send the first message to start this conversation.</p></div>
  const streamActive = Boolean(streamingTurn)
  return <ol className="transcript" aria-label="Transcript">{rendered.map((turn) => <TurnView key={turn.id} turn={turn} onRetry={onRetry} onBranch={onBranch} streamActive={streamActive} conversationId={conversationId} autoplayPreference={autoplayPreference} />)}</ol>
}

function Composer({ draft, setDraft, onSend, onCancel, streaming, disabled, canSend, voiceControl }) {
  const textareaRef = useRef(null)
  const cancelRef = useRef(null)
  const wasStreamingRef = useRef(false)
  const onKeyDown = (event) => {
    // Do not submit on an IME commit Enter (a11y #8): while composing CJK/other
    // input, the terminal Enter that accepts a candidate reports isComposing
    // (keyCode 229 on older engines) and must insert/commit, never send.
    if (event.isComposing || event.keyCode === 229) return
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (canSend) onSend() }
  }
  // Focus follows the Send↔Cancel swap (a11y #6): when a stream starts, move
  // focus to Cancel so it is never dropped to <body>; when the stream settles,
  // return focus to the composer so the next message can be typed immediately.
  useEffect(() => {
    if (streaming) cancelRef.current?.focus()
    else if (wasStreamingRef.current) textareaRef.current?.focus()
    wasStreamingRef.current = streaming
  }, [streaming])
  return <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); if (canSend) onSend() }}>
    <textarea ref={textareaRef} aria-label="Message composer" aria-describedby="composer-hint" rows="3" value={draft} disabled={disabled}
      onChange={(event) => setDraft(event.target.value)} onKeyDown={onKeyDown}
      placeholder={disabled ? 'Select or start a conversation to send a message…' : 'Message…  (Enter to send, Shift+Enter for a new line)'} />
    <div className="composer-bar">
      <small id="composer-hint">Enter sends. Shift+Enter inserts a new line.</small>
      {/* The voice affordance sits in the SAME action row as Send, immediately to
          its left. It is dictation only (record → editable transcript → explicit
          Send), never the realtime speech-to-speech loop — that lives in its own
          Realtime voice panel. Placing it here keeps the dictation control next to
          the primary Send action instead of a detached banner row above. */}
      <div className="composer-actions">
        {voiceControl}
        {streaming
          ? <button ref={cancelRef} type="button" className="composer-cancel" aria-label="Cancel streaming response" onClick={onCancel}>Cancel</button>
          : <button type="submit" aria-label="Send message" disabled={!canSend}>Send <span aria-hidden="true">↵</span></button>}
      </div>
    </div>
  </form>
}

function RouteSelect({ routes, routeId, onChange }) {
  return <label className="chat-route"><span>Route</span>
    <select aria-label="Chat route" value={routeId || ''} onChange={(event) => onChange(event.target.value)} disabled={routes.length === 0}>
      {routes.length === 0 && <option value="">No routes configured</option>}
      {routes.map((route) => <option key={route.route_id} value={route.route_id}>{route.display_name || route.route_id}</option>)}
    </select>
  </label>
}

// --- Voice push-to-talk + read-aloud (chat-first-voice T005.2 / T005.3) -------
//
// The IMPURE edges of the voice slice: microphone capture and audio playback.
// The record/draft and playback STATE lives in the pure reducers in chat-api.js;
// these components only wire the browser media APIs to those reducers. Both
// degrade truthfully when the relay is unconfigured (a 503 becomes textual error
// state) and NEVER block the textual chat.

// Push-to-talk: hold to record, release to transcribe into an EDITABLE draft
// dropped into the composer. It NEVER sends a turn — the actor reviews, edits,
// and submits through the ordinary composer. Permission denial or an STT failure
// is a non-blocking textual error; the composer stays usable throughout.
function PushToTalk({ conversationId, onDraft, disabled }) {
  const [state, dispatch] = useReducer(voiceInputReducer, undefined, initialVoiceInputState)
  // Captured Float32 sample chunks (copies) for the whole hold. Wrapped in a WAV
  // header on interim/final so the Dark STT serve — which accepts a real WAV and
  // has no server-side transcoder — reads honest 16 kHz mono PCM16.
  const samplesRef = useRef([])
  const captureRef = useRef(null)
  const mountedRef = useRef(true)
  // True only during the hold (press→release). An interim transcription is
  // emitted ONLY while this is set, so a chunk delivered on stop cannot fire a
  // stray interim after release, and the reducer's own `listening`-guard is a
  // second backstop against an out-of-order interim landing on a settled draft.
  const recordingRef = useRef(false)
  // Throttle interim captions: an onaudioprocess block fires ~4x/second, but the
  // relay is request/response, so we transcribe accumulated audio at most this
  // often. Starts at 0 so the first captured block emits an interim promptly.
  const lastInterimRef = useRef(0)
  const interimInFlightRef = useRef(false)
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; recordingRef.current = false; releaseCapture() } }, [])

  const releaseCapture = () => {
    const capture = captureRef.current
    if (!capture) return
    try { capture.processor.disconnect() } catch { /* already gone */ }
    try { capture.source.disconnect() } catch { /* already gone */ }
    capture.stream?.getTracks?.().forEach((track) => { try { track.stop() } catch { /* already stopped */ } })
    try { capture.context.close?.() } catch { /* already closing */ }
    captureRef.current = null
  }

  // Concatenate accumulated Float32 chunks and wrap them as a base64 WAV. NEVER a
  // turn — the bytes are relayed to STT and dropped; no audio is stored anywhere.
  const accumulatedWavBase64 = () => {
    const chunks = samplesRef.current
    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
    const merged = new Float32Array(total)
    let offset = 0
    for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.length }
    return float32ToWavBase64(merged, VOICE_RELAY_SAMPLE_RATE)
  }

  // Provisional caption: transcribe the audio captured SO FAR as a non-final
  // (`isFinal:false`) draft and render it live while the actor still holds. It is
  // NOT a committable draft and NEVER a turn; the reducer keeps it only while
  // `listening`, and the release-time final pass supersedes it. A failed interim
  // is silent — the final pass still runs and textual chat is never blocked.
  const emitInterim = async () => {
    if (!recordingRef.current || interimInFlightRef.current || !samplesRef.current.length) return
    interimInFlightRef.current = true
    lastInterimRef.current = Date.now()
    try {
      const audioBase64 = accumulatedWavBase64()
      const value = await transcribeVoice({ conversationId, audioBase64, audioFormat: 'wav', isFinal: false })
      if (!mountedRef.current || !recordingRef.current) return
      dispatch({ type: 'interim', text: value?.draft?.text || '' })
    } catch {
      /* a provisional caption failure is non-blocking; the final pass still runs */
    } finally {
      interimInFlightRef.current = false
    }
  }

  const finish = async () => {
    try {
      const audioBase64 = accumulatedWavBase64()
      const value = await transcribeVoice({ conversationId, audioBase64, audioFormat: 'wav', isFinal: true })
      if (!mountedRef.current) return
      const text = value?.draft?.text || ''
      // The interim caption settles into the EDITABLE final draft in the composer.
      dispatch({ type: 'final', text })
      onDraft?.(text) // populate the EDITABLE composer; no turn is sent here
    } catch (error) {
      if (mountedRef.current) dispatch({ type: 'error', message: error?.message || 'Your recording could not be transcribed.' })
    } finally {
      samplesRef.current = []
    }
  }

  const start = async () => {
    if (disabled || !conversationId || state.status === 'listening') return
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) {
      dispatch({ type: 'error', message: 'This browser cannot capture audio. You can still type your message.' })
      return
    }
    // Held across the try/catch so a throw AFTER the mic is acquired (e.g. a
    // fixed-rate AudioContext NotSupportedError on Firefox) can still stop the
    // stream's tracks directly — releaseCapture() alone would early-return while
    // captureRef is still null and leave the mic indicator hot.
    let stream = null
    try {
      // Capture 16 kHz mono PCM16 through the SAME rate-honest AudioContext path
      // the realtime relay uses; the accumulated samples are wrapped in a WAV
      // header on release (dictation is request/response, not a socket).
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const context = new window.AudioContext({ sampleRate: VOICE_RELAY_SAMPLE_RATE })
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      samplesRef.current = []
      lastInterimRef.current = 0
      interimInFlightRef.current = false
      recordingRef.current = true
      processor.onaudioprocess = (event) => {
        if (!recordingRef.current) return
        // Copy the reused input buffer — the underlying Float32Array is recycled.
        samplesRef.current.push(Float32Array.from(event.inputBuffer.getChannelData(0)))
        // A throttled interim caption while holding; superseded by the final draft.
        if (Date.now() - lastInterimRef.current >= 900) emitInterim()
      }
      source.connect(processor); processor.connect(context.destination)
      captureRef.current = { stream, context, source, processor }
      dispatch({ type: 'press' })
    } catch {
      // Non-blocking: no audio left this browser. Stop any acquired mic tracks
      // directly so the mic never stays hot when capture setup failed mid-way.
      recordingRef.current = false
      stream?.getTracks?.().forEach((track) => { try { track.stop() } catch { /* already stopped */ } })
      dispatch({ type: 'error', message: 'Microphone access was not granted. You can still type your message.' })
      releaseCapture()
    }
  }

  const stop = () => {
    if (state.status !== 'listening') return
    // Clear the hold flag BEFORE tearing down so a late audio block cannot fire a
    // stray interim after release.
    recordingRef.current = false
    dispatch({ type: 'release' })
    releaseCapture()
    finish()
  }

  const listening = state.status === 'listening'
  return <div className="voice-ptt">
    <button type="button" className={`ptt-button ${listening ? 'listening' : ''}`}
      aria-label="Hold to talk" aria-pressed={listening} disabled={disabled || !conversationId}
      onPointerDown={start} onPointerUp={stop} onPointerCancel={stop} onPointerLeave={stop}
      onKeyDown={(event) => { if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) { event.preventDefault(); start() } }}
      onKeyUp={(event) => { if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); stop() } }}>
      {listening ? 'Listening… release to review' : 'Hold to talk'}
    </button>
    {/* Live provisional caption while holding: rendered as its own aria-live
        region, superseded by the editable final draft in the composer on release. */}
    {state.interim && <span className="voice-ptt-caption" role="status" aria-live="polite" aria-label="Interim transcript">{state.interim}</span>}
    <span className="voice-ptt-status" role="status" aria-live="polite">{voiceInputLabel(state)}</span>
  </div>
}

// Singleton playback registry: at most ONE ReadAloud may play at a time. Each
// message renders its own <audio>, so without this a second Play would layer over
// the first (overlapping audio). Claiming playback interrupts whoever held it —
// pausing that audio and dispatching `interrupt` to reset its reducer — so message
// B supersedes message A cleanly. Interrupt changes ONLY transient playback state;
// no message or conversation state is touched (the no-mutation guarantee holds).
let _activePlayback = null
function claimPlayback(entry) {
  if (_activePlayback && _activePlayback !== entry) _activePlayback.interrupt()
  _activePlayback = entry
}
function releasePlayback(entry) {
  if (_activePlayback === entry) _activePlayback = null
}

// Build the <audio> source URI from a read-aloud response. Raw PCM16 (the Dark
// TTS serve's native output) carries no container, so it is wrapped in a WAV
// header at the serve-REPORTED sample rate (kokoro's 24 kHz, not the 16 kHz
// capture rate) — playing it at a wrong rate is the same garble the realtime fix
// addressed. Any already-containered format (mp3/wav/opus) passes straight through.
function audioSourceFor(value) {
  const format = value?.audio_format || 'mp3'
  const base64 = value?.audio_base64 || ''
  if (format === 'pcm16' || format === 'pcm') {
    const rate = Number.isFinite(value?.sample_rate) && value.sample_rate > 0 ? value.sample_rate : 24000
    return pcm16Base64ToWavDataUri(base64, rate)
  }
  return `data:audio/${format};base64,${base64}`
}

// Read-aloud: transient TTS playback for one assistant response. Playback moves
// only local audio status — it NEVER changes the message or conversation. The
// text stays available before/during/after audio. Autoplay is OPTIONAL and only
// fires when the operator's saved preference opts in.
function ReadAloud({ conversationId, turn, autoplayPreference }) {
  const [state, dispatch] = useReducer(playbackReducer, undefined, initialPlaybackState)
  const audioRef = useRef(null)
  const mountedRef = useRef(true)
  // Stable registry entry for the singleton. `interrupt` stops THIS component's
  // audio and resets its reducer when another message supersedes it. Refs it
  // reads (audioRef, dispatch, mountedRef) are stable, so capturing once is safe.
  const entryRef = useRef(null)
  if (!entryRef.current) {
    entryRef.current = {
      interrupt: () => {
        try { audioRef.current?.pause() } catch { /* nothing playing */ }
        if (mountedRef.current) dispatch({ type: 'interrupt' })
      },
    }
  }

  const teardown = () => {
    const audio = audioRef.current
    if (audio) { try { audio.pause() } catch { /* nothing playing */ } audioRef.current = null }
  }

  const play = async () => {
    const text = turnText(turn)
    if (!text.trim() || !conversationId) return
    dispatch({ type: 'load', messageRef: turn.id })
    try {
      const value = await speakMessage({ conversationId, messageRef: turn.id, text, outputFormat: 'pcm16' })
      if (!mountedRef.current) return
      teardown()
      const audio = new window.Audio(audioSourceFor(value))
      audioRef.current = audio
      audio.onended = () => { if (mountedRef.current) dispatch({ type: 'ended' }); releasePlayback(entryRef.current) }
      audio.onerror = () => { if (mountedRef.current) dispatch({ type: 'error', message: 'Audio playback failed.' }); releasePlayback(entryRef.current) }
      // Claim the singleton BEFORE starting: this interrupts any other message's
      // in-progress playback so the two never overlap.
      claimPlayback(entryRef.current)
      const started = audio.play?.()
      if (started?.catch) started.catch(() => { if (mountedRef.current) dispatch({ type: 'error', message: 'Audio playback failed.' }) })
      dispatch({ type: 'play' })
    } catch (error) {
      if (mountedRef.current) dispatch({ type: 'error', message: error?.message || 'Read-aloud could not be produced.' })
    }
  }

  const pause = () => { try { audioRef.current?.pause() } catch { /* nothing playing */ } dispatch({ type: 'pause' }) }
  const resume = () => { const started = audioRef.current?.play?.(); started?.catch?.(() => {}); claimPlayback(entryRef.current); dispatch({ type: 'resume' }) }
  const stop = () => { const audio = audioRef.current; if (audio) { try { audio.pause(); audio.currentTime = 0 } catch { /* noop */ } } releasePlayback(entryRef.current); dispatch({ type: 'stop' }) }
  const replay = () => { const audio = audioRef.current; if (audio) { try { audio.currentTime = 0; audio.play?.() } catch { /* noop */ } } if (audioRef.current) { claimPlayback(entryRef.current); dispatch({ type: 'replay', messageRef: turn.id }) } else { play() } }

  useEffect(() => {
    mountedRef.current = true
    // Optional autoplay: only when the saved preference opts in, and only for a
    // FRESHLY-ARRIVED completed assistant response (`turn.fresh`) — never for the
    // historical turns that mount when a conversation is opened, which would
    // otherwise autoplay every past message at once. A new response arriving is
    // the one moment autoplay is expected.
    if (shouldAutoplay(autoplayPreference) && turn.fresh && turn.role === 'assistant' && turn.status === 'complete') play()
    const entry = entryRef.current
    return () => { mountedRef.current = false; teardown(); releasePlayback(entry) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const active = isPlaybackActiveFor(state, turn.id)
  return <span className="read-aloud">
    {!active
      ? <button type="button" aria-label="Read this response aloud" onClick={play}>Play audio</button>
      : <>
          {state.status === 'playing' && <button type="button" aria-label="Pause audio" onClick={pause}>Pause</button>}
          {state.status === 'paused' && <button type="button" aria-label="Resume audio" onClick={resume}>Resume</button>}
          {(state.status === 'playing' || state.status === 'paused') && <button type="button" aria-label="Stop audio" onClick={stop}>Stop</button>}
        </>}
    <button type="button" aria-label="Replay audio" onClick={replay} disabled={state.status === 'loading'}>Replay</button>
    <span className="read-aloud-status" role="status" aria-live="polite">{playbackLabel(state)}</span>
  </span>
}

function ChatView({ append }) {
  const [conversations, setConversations] = useState([])
  // Saved read-aloud autoplay preference, loaded from the REAL served
  // `/api/preferences` surface and adapted into the `{voice_autoplay}` shape
  // `shouldAutoplay` reads. Default OFF until it resolves, and OFF if the surface
  // is 503/absent — autoplay never fires on a preference the operator did not set.
  const [voicePreferences, setVoicePreferences] = useState({ voice_autoplay: false })
  const [includeArchived, setIncludeArchived] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [turns, setTurns] = useState([])
  const [routes, setRoutes] = useState([])
  const [routeId, setRouteId] = useState('')
  // Route-resolution provenance (chat-first-voice T010): a route the actor
  // EXPLICITLY picks vs one DEFAULTED from preference. It is sent so Serving can
  // echo it back on the turn's resolution mark; the displayed provenance always
  // comes from that served mark, never this local hint alone.
  const [routeProvenance, setRouteProvenance] = useState('preference_default')
  // The current divergence notice (one per episode) + the episode ids already
  // announced, so the notice appears EXACTLY ONCE per divergence episode and is
  // non-blocking (a dismissible status line; the composer stays usable).
  const [divergence, setDivergence] = useState(null)
  const announcedEpisodesRef = useRef([])
  const [advanced, setAdvanced] = useState(false)
  // Advanced playground state (advanced-model-playground T005). Routes are the
  // reviewed advanced-route allowlist; a 503/unconfigured surface degrades
  // truthfully to `advUnavailable` and the ordinary transcript stays usable. A
  // branch is a settled `mode="advanced"` SIBLING turn in the SAME transcript —
  // never a second transcript — plus its redacted trace for the inspector.
  const [advRoutes, setAdvRoutes] = useState([])
  const [advUnavailable, setAdvUnavailable] = useState('')
  const [advBranches, setAdvBranches] = useState([])
  const [advInspectingId, setAdvInspectingId] = useState(null)
  const [advCompareIds, setAdvCompareIds] = useState([])
  // Advanced playground extensions (advanced-model-playground T006/T009/T010):
  // actor-private presets, instruction templates, and declared-criterion route
  // ratings. Each surface fails closed (503) to a shared truthful unavailable
  // sentinel and the ordinary transcript stays usable.
  const [playgroundUnavailable, setPlaygroundUnavailable] = useState('')
  const [advPresets, setAdvPresets] = useState([])
  const [advTemplates, setAdvTemplates] = useState([])
  const [ratingCriteria, setRatingCriteria] = useState([])
  const [ratingAggregates, setRatingAggregates] = useState(null)
  const [draft, setDraft] = useState('')
  const [streamingTurn, setStreamingTurn] = useState(null)
  const [lifecycle, setLifecycle] = useState('')
  const [renamingId, setRenamingId] = useState(null)
  const [confirmingDeleteId, setConfirmingDeleteId] = useState(null)
  const abortRef = useRef(null)
  // The LIVE parallel dispatch runs N branches at once and is NOT gated on the
  // single `streamingTurn`/`abortRef` slot; it owns its OWN batch AbortController so
  // a new dispatch (or an unmount) can tear down an in-flight batch, which the
  // server observes to settle ALL N branches cancelled.
  const dispatchAbortRef = useRef(null)
  const seqRef = useRef(0)
  // Latest-wins guard for the conversation list/search (a11y #9): every fetch
  // claims a monotonic ticket; a resolved fetch applies its result only if it is
  // still the newest, so a slow earlier query can never overwrite a newer one.
  const listSeqRef = useRef(0)
  const listAbortRef = useRef(null)
  const railRef = useRef(null)
  // Mirror of the selected id readable from an async settle without a stale
  // closure (a11y #2): an in-flight send captured its conversation id and
  // compares it against this on settle to drop a result for a switched-away
  // conversation instead of mutating the now-current one.
  const selectedIdRef = useRef(null)

  const selected = conversations.find((record) => record.id === selectedId) || null

  const refreshList = async (nextQuery, nextArchived, signal) => {
    const seq = (listSeqRef.current += 1)
    try {
      const value = nextQuery && nextQuery.trim()
        ? await searchConversations(nextQuery.trim(), { includeArchived: nextArchived, signal })
        : await listConversations({ includeArchived: nextArchived, signal })
      if (seq !== listSeqRef.current) return // a newer list/search superseded this one
      setConversations(value.conversations || [])
    } catch (error) {
      if (signal?.aborted || error?.name === 'AbortError') return // superseded fetch aborted
      if (seq === listSeqRef.current) append('Conversations are unavailable. No conversation content left the tailnet.')
    }
  }
  // List/search fetch with debounce + abort (a11y #9): each run aborts the prior
  // in-flight request, then a non-empty search waits ~150ms so a fast typist
  // makes one request per pause; the plain list and the archived toggle refresh
  // immediately. The latest-wins guard in refreshList is the final backstop.
  useEffect(() => {
    const controller = new AbortController()
    listAbortRef.current?.abort()
    listAbortRef.current = controller
    if (!query.trim()) { refreshList(query, includeArchived, controller.signal); return undefined }
    const handle = setTimeout(() => { refreshList(query, includeArchived, controller.signal) }, 150)
    return () => { clearTimeout(handle) }
  }, [query, includeArchived])
  useEffect(() => {
    fetchChatRoutes()
      .then((value) => { const list = value.routes || []; setRoutes(list); setRouteId((current) => current || list[0]?.route_id || '') })
      .catch(() => setRoutes([]))
  }, [])
  // Load the saved read-aloud autoplay preference from the served
  // `/api/preferences` payload via the adapter. Tolerant of a 503/absent surface:
  // any failure keeps the default-OFF state so autoplay is never surprising.
  useEffect(() => {
    fetchPreferences()
      .then((payload) => setVoicePreferences(voiceAutoplayFromPreferences(payload)))
      .catch(() => setVoicePreferences({ voice_autoplay: false }))
  }, [])
  // Load the reviewed advanced-route allowlist. NOTE (S6): the advanced HTTP
  // surface — GET /api/chat/advanced/routes and POST
  // /api/conversations/{id}/advanced/run — is
  // NOT yet wired server-side. T005 is proven at the COMPONENT level over the
  // merged serializer shapes (route/control/branch/advanced-trace.v1), and the
  // live path degrades to `advUnavailable` (503 → the shared not-configured
  // sentinel) pending that backend wiring under AMP:T007 (live qualification). So
  // a 503 or any failure sets a truthful unavailable state — the panel degrades
  // and the ordinary transcript is never blocked.
  useEffect(() => {
    fetchAdvancedRoutes()
      .then((value) => { setAdvRoutes(value.routes || []); setAdvUnavailable('') })
      .catch((error) => {
        setAdvRoutes([])
        setAdvUnavailable(error?.message === ADVANCED_NOT_CONFIGURED ? ADVANCED_NOT_CONFIGURED : 'Advanced controls are unavailable for this hub.')
      })
  }, [])

  // Load the actor-private playground surfaces (presets / templates / rating
  // criteria + aggregates). Each fails closed with 503 → a truthful unavailable
  // sentinel; the transcript is never blocked. Read-only on mount; writes happen
  // through the callbacks below.
  const refreshRatingAggregates = () => {
    fetchRatingAggregates().then(setRatingAggregates).catch(() => {})
  }
  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetchAdvancedPresets().then((value) => value.presets || []),
      fetchAdvancedTemplates().then((value) => value.templates || []),
      fetchRatingCriteria().then((value) => value.criteria || []),
      fetchRatingAggregates(),
    ])
      .then(([presets, templates, criteria, aggregates]) => {
        if (cancelled) return
        setAdvPresets(presets); setAdvTemplates(templates)
        setRatingCriteria(criteria); setRatingAggregates(aggregates)
        setPlaygroundUnavailable('')
      })
      .catch((error) => {
        if (cancelled) return
        setAdvPresets([]); setAdvTemplates([]); setRatingCriteria([]); setRatingAggregates(null)
        setPlaygroundUnavailable(
          error?.message === ADVANCED_PLAYGROUND_NOT_CONFIGURED
            ? ADVANCED_PLAYGROUND_NOT_CONFIGURED
            : 'The advanced playground extensions are unavailable for this hub.',
        )
      })
    return () => { cancelled = true }
  }, [])

  // Resolve a preset for selection. The server derives the live route / profile /
  // tool / response-schema digests from its OWN registry and decides ready /
  // repair / unverifiable; the browser supplies NO digests, so it cannot spoof a
  // drifted pin to "ready" nor produce false drift from its partial route view.
  const resolvePlaygroundPreset = (presetId) => resolveAdvancedPreset(presetId)

  // Build a FACTUAL comparison from the settled advanced branches. A ranking is
  // representable only alongside a declared criterion (server-enforced), so this
  // assembles metrics only and never an inferred winner. Only SETTLED branches
  // that produced a trace are real, comparable attempts: an in-flight/unsettled
  // branch (or one that never settled a trace) is EXCLUDED rather than fabricated
  // as `complete`, and each included attempt carries the branch's REAL settled
  // status (complete / cancelled / interrupted / failed), so the labels stay
  // factual — a cancelled or failed attempt is never mislabelled a completed one.
  const buildPlaygroundComparison = () => {
    const settled = advBranches.filter((branch) => isBranchSettled(branch) && branch.trace)
    const attempts = settled.slice(0, 4).map((branch) => ({
      turn_id: branch.turnId,
      route: {
        provider: 'anvil-serving',
        route_id: branch.trace?.route_decision?.route_id,
        route_digest: branch.trace?.route_decision?.route_digest,
      },
      status: comparisonAttemptStatus(branch.status),
      metrics: {
        output_tokens: branch.trace?.usage?.output_tokens || 0,
        latency_ms: branch.trace?.usage?.latency_ms || 0,
      },
    }))
    const record = {
      schema_version: 'workbench-advanced-comparison/v1',
      comparison_id: `advcompare_local_${Date.now()}`,
      conversation_id: selectedId,
      fork_point: { parent_turn_id: settled[0]?.trace?.branch_ref?.turn_id },
      attempts,
      created_at: new Date().toISOString(),
    }
    return buildAdvancedComparison(record)
  }

  const resolvePlaygroundTemplate = (templateId, pinnedDigest) => resolveAdvancedTemplate(templateId, pinnedDigest)
  const previewPlaygroundDeclared = (templateId, bindings) => renderAdvancedDeclaredInstructions(templateId, bindings)
  const recordPlaygroundRating = (payload) =>
    recordAdvancedRating(payload).then((result) => { refreshRatingAggregates(); return result })

  // Focus a sensible target after a row leaves the rail (a11y #6): the first
  // remaining conversation, else the "New" affordance — never <body>.
  const focusRail = () => {
    const rail = railRef.current
    if (!rail) return
    const target = rail.querySelector('.conv-open') || rail.querySelector('.conv-new')
    target?.focus()
  }

  const select = async (id) => {
    // Switching conversations aborts any in-flight stream so its settled answer
    // cannot land in — or announce for — the newly selected conversation (#2).
    // Both the single-turn stream AND an in-flight advanced dispatch batch are
    // aborted, so a backgrounded batch can't keep running (and holding its slot).
    abortRef.current?.abort()
    dispatchAbortRef.current?.abort()
    selectedIdRef.current = id
    setSelectedId(id); setStreamingTurn(null); setLifecycle(''); setRenamingId(null); setConfirmingDeleteId(null)
    try { const value = await getConversation(id); if (selectedIdRef.current === id) setTurns(value.turns || []) }
    catch { if (selectedIdRef.current === id) { setTurns([]); append('That conversation could not be opened.') } }
  }
  const newConversation = async () => {
    try { const record = await createConversation({}); setConversations((current) => [record, ...current]); await select(record.id) }
    catch { append('A new conversation could not be created.') }
  }
  const rename = async (id, title) => {
    setRenamingId(null)
    try { const record = await renameConversation(id, title); setConversations((current) => current.map((item) => (item.id === id ? record : item))) }
    catch { append('The conversation could not be renamed.') }
  }
  const archive = async (id) => { try { await archiveConversation(id); await refreshList(query, includeArchived); focusRail() } catch { append('The conversation could not be archived.') } }
  const unarchive = async (id) => { try { await unarchiveConversation(id); await refreshList(query, includeArchived); focusRail() } catch { append('The conversation could not be unarchived.') } }
  const remove = async (id) => {
    setConfirmingDeleteId(null)
    try {
      await deleteConversation(id)
      if (selectedId === id) { abortRef.current?.abort(); dispatchAbortRef.current?.abort(); selectedIdRef.current = null; setSelectedId(null); setTurns([]); setStreamingTurn(null); setLifecycle('') }
      await refreshList(query, includeArchived); focusRail()
    }
    catch { append('The conversation could not be deleted.') }
  }

  const send = async () => {
    const prompt = draft.trim()
    if (!prompt || !selectedId || !routeId || streamingTurn) return
    try { selectChatRoute(routes, routeId) } catch { append('That route is not in the reviewed allowlist.'); return }
    // Bind this send to the conversation it started in; a settle for a
    // switched-away conversation is dropped rather than mutating the current one.
    const conversationId = selectedId
    const isCurrent = () => selectedIdRef.current === conversationId
    const ordinal = (seqRef.current += 1)
    const userTurn = { id: `local-user-${ordinal}`, role: 'user', status: 'complete', content: [{ text: prompt }], lineage: { kind: 'initial' } }
    setTurns((current) => [...current, userTurn]); setDraft('')
    const assistant = { id: `local-assistant-${ordinal}`, role: 'assistant', status: 'streaming', content: [{ text: '' }], lineage: { kind: 'initial' } }
    setStreamingTurn(assistant); setLifecycle(LIFECYCLE.streaming)
    const controller = new AbortController(); abortRef.current = controller
    try {
      const state = await sendMessage({
        conversationId, routeId, routeProvenance, prompt, signal: controller.signal,
        onState: (streamState) => { if (!isCurrent()) return; setStreamingTurn((current) => (current ? {
          ...current, content: [{ text: streamState.text }],
          status: streamState.terminal ? terminalToStatus(streamState.terminal) : 'streaming',
        } : current)) },
      })
      if (!isCurrent()) return // switched away mid-stream: drop this settle entirely
      const status = terminalToStatus(state.terminal)
      // The SURFACE-ONLY route-resolution mark Serving reported for this turn
      // (requested vs served route + provenance); the client never picked a route.
      const resolution = state.routeResolution ?? null
      // `fresh` marks this as a newly-arrived response so ReadAloud may autoplay
      // it (when the saved preference opts in); historical turns never carry it.
      // Adopt the server's persisted assistant turn_id (the terminal carries it)
      // so an immediate fork -- Branch / Retry / advanced Run branch -- references
      // the real turn, not this optimistic local id (which the server 404/409s).
      setTurns((current) => [...current, { ...assistant, id: state.turnId || assistant.id, content: [{ text: state.text }], status, fresh: true, routeResolution: resolution }])
      setStreamingTurn(null)
      setLifecycle(LIFECYCLE[status] || LIFECYCLE.complete)
      // Show the divergence notice EXACTLY ONCE per episode: the pure decision
      // returns null when the turn did not diverge or its episode was already
      // announced. Non-blocking — it only sets a dismissible status line.
      const announcement = divergenceAnnouncement(announcedEpisodesRef.current, resolution)
      if (announcement) {
        if (announcement.episodeId) announcedEpisodesRef.current = [...announcedEpisodesRef.current, announcement.episodeId]
        setDivergence(announcement)
      }
      if (state.needsRefresh) {
        try { const value = await getConversation(conversationId); if (isCurrent()) setTurns(value.turns || []) }
        catch { if (isCurrent()) append('The reconnected transcript could not be refreshed.') }
      }
    } catch {
      if (!isCurrent()) return
      setStreamingTurn(null)
      setTurns((current) => [...current, { ...assistant, status: 'failed', content: [{ text: '' }] }])
      setLifecycle(LIFECYCLE.failed)
      append('The response failed. No partial answer was recorded as complete.')
    } finally {
      // Only clear the ref if this send still owns it — a newer stream may have
      // taken it while this one was settling (#2), and must not be wiped.
      if (abortRef.current === controller) abortRef.current = null
    }
  }
  const cancel = () => { abortRef.current?.abort() }

  // Run one tuned Advanced attempt. It forks a `mode="advanced"` SIBLING into the
  // SAME transcript (the shared `turns` / `streamingTurn` slot) — never a second
  // transcript — and streams through `runAdvancedBranch` with a real
  // AbortController threaded to the fetch, so the panel's Cancel genuinely aborts
  // the in-flight request. On settle it records the branch + its redacted trace for
  // inspect/compare/save/reopen.
  const runAdvancedFromConfig = async ({ route, routeId: advRouteId, values, prompt, instructions, label }) => {
    if (!selectedId || streamingTurn || !route) return
    const conversationId = selectedId
    const isCurrent = () => selectedIdRef.current === conversationId
    const ordinal = (seqRef.current += 1)
    const branchLocalId = `advbranch-${ordinal}`
    // The panel's Run does not name a branch; give every branch a stable, readable
    // default label so its row + per-branch control names are meaningful (never
    // "undefined"). A rerun/fork carries the originating label forward.
    const branchLabel = label || `Branch ${ordinal}`
    const parentTurnId = turns.length ? turns[turns.length - 1].id : null
    const controls = submittedControls(route, values)
    const userTurn = { id: `local-adv-user-${ordinal}`, role: 'user', status: 'complete', content: [{ text: prompt }], lineage: { kind: 'branch' }, mode: 'advanced' }
    setTurns((current) => [...current, userTurn])
    const assistant = { id: `local-adv-${ordinal}`, role: 'assistant', status: 'streaming', content: [{ text: '' }], lineage: { kind: 'branch' }, mode: 'advanced' }
    setStreamingTurn(assistant); setLifecycle('Advanced branch streaming')
    setAdvBranches((current) => [...current, { id: branchLocalId, label: branchLabel, routeId: advRouteId, controlsValues: values, prompt, instructions, status: 'streaming', text: '', trace: null, saved: false }])
    const controller = new AbortController(); abortRef.current = controller
    let capturedTrace = null
    try {
      const state = await runAdvancedBranch({
        conversationId, parentTurnId, branchId: branchLocalId, routeId: advRouteId, controls, prompt, instructions,
        signal: controller.signal,
        onFrame: (frame) => { if (frame.trace) capturedTrace = frame.trace },
        onState: (streamState) => { if (!isCurrent()) return; setStreamingTurn((current) => (current ? {
          ...current, content: [{ text: streamState.text }],
          status: streamState.terminal ? terminalToStatus(streamState.terminal) : 'streaming',
        } : current)) },
      })
      if (!isCurrent()) return
      const status = terminalToStatus(state.terminal)
      const trace = state.trace || capturedTrace
      // Adopt the server's persisted assistant turn_id (the advanced-run terminal
      // carries it) over this optimistic local id, so a follow-on Retry / Branch /
      // chained Run on this advanced turn references the real server turn -- not a
      // local id the server 404/409s (mirrors the ordinary send-completion fix).
      const settledTurn = { ...assistant, id: state.turnId || assistant.id, content: [{ text: state.text }], status, fresh: true }
      setTurns((current) => [...current, settledTurn]); setStreamingTurn(null)
      setLifecycle(`Advanced branch ${status}`)
      setAdvBranches((current) => current.map((branch) => (branch.id === branchLocalId
        ? { ...branch, status, text: state.text, trace, turnId: settledTurn.id, branchId: state.branchId || branchLocalId }
        : branch)))
    } catch {
      if (!isCurrent()) return
      setStreamingTurn(null)
      setTurns((current) => [...current, { ...assistant, status: 'failed', content: [{ text: '' }] }])
      setLifecycle('Advanced branch failed')
      setAdvBranches((current) => current.map((branch) => (branch.id === branchLocalId ? { ...branch, status: 'failed' } : branch)))
      append('The advanced branch failed. No partial attempt was recorded as complete.')
    } finally {
      if (abortRef.current === controller) abortRef.current = null
    }
  }
  // Run ONE shared prompt across N reviewed routes CONCURRENTLY. Unlike the single
  // Run branch above this is NOT gated on the one `streamingTurn`/`abortRef` slot:
  // it forks N `mode="advanced"` siblings under the SAME parent, tracks a row per
  // branch keyed by the server `branch_id`, streams each live from `onBranchState`,
  // and on settle adopts each branch's server `turn_id` (so a follow-on Retry/Fork/
  // Run on any of them references the real server turn). All N land in `advBranches`
  // so the EXISTING "Build comparison" works over them unchanged. One AbortController
  // covers the whole batch.
  const runAdvancedDispatchFromConfig = async ({ routeIds, prompt, instructions }) => {
    if (!selectedId || !Array.isArray(routeIds) || routeIds.length < 2) return
    const conversationId = selectedId
    const isCurrent = () => selectedIdRef.current === conversationId
    const parentTurnId = turns.length ? turns[turns.length - 1].id : null
    const ordinal = (seqRef.current += 1)
    // One shared user turn shows the fanned-out prompt; the N assistant siblings
    // stream below it.
    const userTurn = { id: `local-adv-dispatch-user-${ordinal}`, role: 'user', status: 'complete', content: [{ text: prompt }], lineage: { kind: 'branch' }, mode: 'advanced' }
    setTurns((current) => [...current, userTurn])
    const routesPayload = routeIds.map((id) => ({ route_id: id, controls: {} }))
    // Track only THIS batch's ids so the settle/error paths never touch a sibling
    // single-run's rows.
    const batchBranchIds = new Set()
    const batchTurnIds = new Set()
    dispatchAbortRef.current?.abort()
    const controller = new AbortController(); dispatchAbortRef.current = controller
    setLifecycle(`Parallel dispatch streaming across ${routeIds.length} routes`)
    try {
      const results = await runAdvancedDispatch({
        conversationId, parentTurnId, routes: routesPayload, prompt, instructions,
        signal: controller.signal,
        onDispatch: (announced) => {
          if (!isCurrent()) return
          announced.forEach((b) => { batchBranchIds.add(b.branch_id); batchTurnIds.add(b.turn_id) })
          // Append a streaming assistant sibling per branch (keyed by the SERVER
          // turn id the dispatch frame already carries) and a matching branch row.
          setTurns((current) => [...current, ...announced.map((b) => ({
            id: b.turn_id, role: 'assistant', status: 'streaming', content: [{ text: '' }],
            lineage: { kind: 'branch' }, mode: 'advanced',
          }))])
          setAdvBranches((current) => [...current, ...announced.map((b, index) => ({
            id: b.branch_id, label: `Dispatch ${ordinal}.${index + 1}`, routeId: b.route_id,
            controlsValues: {}, prompt, instructions, status: 'streaming', text: '',
            trace: null, turnId: b.turn_id, saved: false,
          }))])
        },
        onBranchState: (branchId, state, { turnId, trace }) => {
          if (!isCurrent()) return
          const status = state.terminal ? terminalToStatus(state.terminal) : 'streaming'
          if (turnId) setTurns((current) => current.map((turn) => (turn.id === turnId
            ? { ...turn, content: [{ text: state.text }], status } : turn)))
          setAdvBranches((current) => current.map((branch) => (branch.id === branchId
            ? { ...branch, status, text: state.text, trace: trace || branch.trace, turnId: turnId || branch.turnId }
            : branch)))
        },
      })
      if (!isCurrent()) return
      // Final settle: adopt each branch's server turn id + terminal status/trace so a
      // follow-on fork references the real server turn, never a local id.
      setAdvBranches((current) => current.map((branch) => {
        const result = results.find((item) => item.branchId === branch.id)
        if (!result) return branch
        return {
          ...branch, status: terminalToStatus(result.terminal), text: result.text,
          trace: result.trace || branch.trace, turnId: result.turnId || branch.turnId, branchId: result.branchId,
        }
      }))
      setTurns((current) => current.map((turn) => {
        const result = results.find((item) => item.turnId === turn.id)
        if (!result) return turn
        return { ...turn, id: result.turnId || turn.id, content: [{ text: result.text }], status: terminalToStatus(result.terminal), fresh: true }
      }))
      setLifecycle(`Parallel dispatch settled across ${results.length} routes`)
    } catch {
      if (!isCurrent()) return
      // Mark only THIS batch's still-streaming branches/turns failed.
      setAdvBranches((current) => current.map((branch) => (batchBranchIds.has(branch.id) && branch.status === 'streaming' ? { ...branch, status: 'failed' } : branch)))
      setTurns((current) => current.map((turn) => (batchTurnIds.has(turn.id) && turn.status === 'streaming' ? { ...turn, status: 'failed' } : turn)))
      setLifecycle('Parallel dispatch failed')
      append('The parallel dispatch failed. No partial attempt was recorded as complete.')
    } finally {
      if (dispatchAbortRef.current === controller) dispatchAbortRef.current = null
    }
  }

  // Retry re-runs an identical attempt; fork runs a variant from the same config —
  // both are new sibling turns in the one transcript (no duplicate transcript).
  const rerunAdvanced = (branch, mode) => {
    const route = advRoutes.find((item) => item.route_id === branch.routeId)
    if (!route) { append('That advanced route is no longer in the reviewed allowlist.'); return }
    runAdvancedFromConfig({
      route, routeId: branch.routeId, values: branch.controlsValues, prompt: branch.prompt,
      instructions: branch.instructions, label: `${branch.label} · ${mode}`,
    })
  }
  const saveAdvancedBranch = (branch) => setAdvBranches((current) => current.map((item) => (item.id === branch.id ? { ...item, saved: true } : item)))
  const reopenAdvancedBranch = (branch) => setAdvBranches((current) => current.map((item) => (item.id === branch.id ? { ...item, saved: true } : item)))
  const toggleAdvancedCompare = (branchId) => {
    if (branchId === null) { setAdvCompareIds([]); return }
    setAdvCompareIds((current) => current.includes(branchId)
      ? current.filter((id) => id !== branchId)
      : [...current, branchId].slice(-2))
  }

  // Retry/branch post ONLY the `{kind:'text', text}` slice the server accepts and
  // pick the role the server's turn tree expects (#1): retry appends a sibling
  // ASSISTANT regeneration; branch opens a follow-up USER turn. Reposting a
  // server-loaded block verbatim carried `content_trust` and was rejected 422.
  const addSuccessor = async (call, turn, role, fallbackKind, failure) => {
    try {
      const created = await call(selectedId, turn.id, successorTurnBody(turn, { role, mode: advanced ? 'advanced' : 'ordinary' }))
      setTurns((current) => [...current, { ...created, lineage: created.lineage || { kind: fallbackKind } }])
      setLifecycle(fallbackKind === 'retry' ? 'Added a retry response' : 'Added a branch response')
    } catch { append(failure) }
  }
  const retry = (turn) => addSuccessor(retryTurn, turn, 'assistant', 'retry', 'Retry could not be recorded.')
  const branch = (turn) => addSuccessor(branchTurn, turn, 'user', 'branch', 'Branch could not be recorded.')

  const streaming = Boolean(streamingTurn)
  const canSend = Boolean(!streaming && draft.trim() && selectedId && routeId)
  return <main className="chat">
    <ConversationRail
      conversations={conversations} selectedId={selectedId} includeArchived={includeArchived} query={query} renamingId={renamingId}
      confirmingDeleteId={confirmingDeleteId} railRef={railRef}
      onSelect={select} onNew={newConversation} onQueryChange={setQuery} onToggleArchived={() => setIncludeArchived((value) => !value)}
      onRename={rename} onArchive={archive} onUnarchive={unarchive} onDelete={remove}
      onStartRename={(id) => { setConfirmingDeleteId(null); setRenamingId(id) }} onCancelRename={() => setRenamingId(null)}
      onRequestDelete={(id) => { setRenamingId(null); setConfirmingDeleteId(id) }} onCancelDelete={() => setConfirmingDeleteId(null)} />
    <section className="chat-main" aria-label="Conversation">
      <header className="chat-header">
        <div><span className="crumb">Chat / private</span><h1>{selected ? describeConversation(selected).title : 'Chat'}</h1></div>
        <div className="chat-controls">
          <RouteSelect routes={routes} routeId={routeId} onChange={(value) => { setRouteId(value); setRouteProvenance('explicit') }} />
          <button className={`advanced-toggle ${advanced ? 'on' : ''}`} aria-pressed={advanced} aria-label="Toggle Advanced mode" onClick={() => setAdvanced((value) => !value)}>Advanced</button>
        </div>
      </header>
      <DeliveryContext context={selected?.context} />
      {advanced && <AdvancedPanel
        unavailable={advUnavailable} routes={advRoutes} streaming={streaming}
        branches={advBranches} onRun={runAdvancedFromConfig} onRunDispatch={runAdvancedDispatchFromConfig}
        onRerun={rerunAdvanced} onCancel={cancel}
        onInspect={setAdvInspectingId} inspectingId={advInspectingId}
        onSave={saveAdvancedBranch} onReopen={reopenAdvancedBranch}
        onToggleCompare={toggleAdvancedCompare} compareIds={advCompareIds} />}
      {advanced && <AdvancedPlaygroundPanel
        unavailable={playgroundUnavailable} routes={advRoutes}
        presets={advPresets} templates={advTemplates} criteria={ratingCriteria} aggregates={ratingAggregates}
        onResolvePreset={resolvePlaygroundPreset} onBuildComparison={buildPlaygroundComparison}
        onResolveTemplate={resolvePlaygroundTemplate} onDeclaredInstructions={previewPlaygroundDeclared}
        onRecordRating={recordPlaygroundRating} />}
      <div className="transcript-scroll"><Transcript selected={selected} turns={turns} streamingTurn={streamingTurn} onRetry={retry} onBranch={branch} conversationId={selectedId} autoplayPreference={voicePreferences} /></div>
      {/* Route-resolution divergence notice (chat-first-voice T010): shown once
          per episode, NON-BLOCKING (a dismissible status line; the composer and
          transcript stay fully usable), surfacing only what Serving reported. */}
      {divergence && <div className="chat-divergence" role="status">
        <span>{divergence.message}</span>
        <button type="button" aria-label="Dismiss route divergence notice" onClick={() => setDivergence(null)}>×</button>
      </div>}
      <div className="chat-live" role="status" aria-live="polite">{lifecycle}</div>
      {/* Push-to-talk (DICTATION) drops an EDITABLE transcript into the composer;
          a turn is sent only when the actor explicitly submits it. It renders
          INLINE in the composer action row, next to Send — not a detached banner
          row above — and is scoped to dictation, distinct from the realtime
          speech-to-speech panel and from per-message read-aloud. */}
      <Composer draft={draft} setDraft={setDraft} onSend={send} onCancel={cancel} streaming={streaming} disabled={!selectedId} canSend={canSend}
        voiceControl={<PushToTalk conversationId={selectedId} onDraft={setDraft} disabled={!selectedId || streaming} />} />
    </section>
  </main>
}

// --- Delivery explorer (plan-task-delivery T003) -----------------------------
//
// A read-only Project → PRD → plan → task explorer over the merged
// delivery-projection GET surface (workbench/api.py build_delivery_projection_router).
// Titles and lineage are the primary visible hierarchy; the scoped id
// `<prd_id>:<task_id>` disambiguates two PRDs' `T001` in navigation AND in the
// URL hash / selection state (R004 / criterion 2). The merged router serves
// per-(project, prd) reads ONLY — there is no served PRD/project enumeration —
// so the explorer lists Projects from bootstrap and opens a PRD by its id,
// rendering a truthful note about the missing enumeration rather than
// fabricating a PRD list. Every load failure (including a 503 when the
// projection is not configured) renders a truthful degraded state.

function ExplorerEligibility({ eligibility }) {
  if (!eligibility || eligibility.status === 'idle' || eligibility.status === 'loading') {
    return <p className="explorer-muted">{eligibility?.status === 'loading' ? 'Checking eligibility…' : 'No eligibility loaded.'}</p>
  }
  if (eligibility.status === 'error') {
    return <p className="explorer-degraded">{eligibility.message || 'Eligibility is unavailable for this task.'}</p>
  }
  const verdict = eligibility.value
  if (!verdict) return <p className="explorer-muted">No eligibility verdict for this task.</p>
  return <div className="explorer-eligibility">
    <Status tone={verdict.eligible ? 'green' : 'amber'}>{verdict.state}</Status>
    <ul>{verdict.reasons.map((reason) => <li key={reason.code}><b>{reason.code}</b> — {reason.explanation}</li>)}</ul>
  </div>
}

function ExplorerTaskRow({ task, selected, onOpen }) {
  // No aria-label here (a11y #1): a button role makes its children presentational
  // for name computation, so an `aria-label` of just the scoped id would REPLACE
  // the visible content and a screen-reader user would hear only the bare id —
  // inverting criterion 1 (title/lineage lead, scoped id is secondary). Letting
  // the accessible name come from content keeps title, State status, delivery
  // status, and scoped id all in the announced name, title-first.
  return <li className={`explorer-task ${selected ? 'selected' : ''}`}>
    <button data-explorer-task={task.scopedId} aria-current={selected ? 'true' : undefined} onClick={() => onOpen(task)}>
      <span className="explorer-task-title">{task.title}</span>
      <span className="explorer-task-meta">
        <Status tone={tone(task.status)}>{task.status}</Status>
        <span className="explorer-pill delivery">{task.latestDeliveryStatus}</span>
      </span>
      <small className="explorer-scoped">{task.scopedId}</small>
    </button>
  </li>
}

function ExplorerPrdCard({ entry, filter, selectedScopedId, onReadPrd, onOpenTask }) {
  const described = (entry.tasks || []).map(describeTaskReference)
  const filtered = filterDescribedTasks(described, filter)
  const heading = entry.prd ? entry.prd.title : entry.prdId
  return <article className="explorer-prd" aria-label={`PRD ${heading}`}>
    <header className="explorer-prd-head">
      <div>
        <h2 className="explorer-prd-title">{heading}</h2>
        <p className="explorer-prd-lineage">{entry.projectName} / {heading}</p>
      </div>
      {entry.prd ? <Status tone={tone(entry.prd.status)}>{entry.prd.status}</Status> : entry.prdError ? <Status tone="amber">unavailable</Status> : null}
    </header>
    {entry.prd && <>
      <dl className="explorer-prd-meta">
        <div><dt>Release</dt><dd>{entry.prd.release || '—'}</dd></div>
        <div><dt>Revision</dt><dd>r{entry.prd.revision ?? '—'}</dd></div>
        <div><dt>Freshness</dt><dd>{freshnessLabel(entry.prd.generatedAt)}</dd></div>
        <div><dt>Progress</dt><dd>{progressSummaryLabel(entry.tasks)}</dd></div>
      </dl>
      <button className="explorer-read" onClick={() => onReadPrd(entry)}>Read PRD content</button>
    </>}
    {entry.prdError && <p className="explorer-degraded">{entry.prdError}</p>}
    {entry.tasksError
      ? <p className="explorer-degraded">{entry.tasksError}</p>
      : <ul className="explorer-task-list" aria-label={`Tasks in ${heading}`}>
          {filtered.length
            ? filtered.map((task) => <ExplorerTaskRow key={task.scopedId} task={task} selected={selectedScopedId === task.scopedId} onOpen={(chosen) => onOpenTask(entry, chosen)} />)
            : <li className="explorer-none">{described.length ? 'No tasks match that filter.' : 'No tasks in this PRD projection.'}</li>}
        </ul>}
  </article>
}

function ExplorerDetail({ selection, reading, eligibility, detailRef, onClose }) {
  if (selection) {
    const task = selection.task
    const lineage = [task.prdTitle, task.featureId, task.scopedId].filter(Boolean).join(' / ')
    // Region name is title-first (a11y #10): `<title> (<scoped id>)` keeps the
    // human title as the primary label and the scoped id as disambiguating
    // secondary disclosure, matching criterion 1.
    return <section className="explorer-detail-pane" aria-label={`${task.title} (${task.scopedId})`} data-scoped-id={task.scopedId}>
      <div className="explorer-detail-topbar"><span className="crumb">{lineage}</span><button className="explorer-close" aria-label="Close" onClick={onClose}>Close</button></div>
      <h2 className="explorer-detail-title" tabIndex={-1} ref={detailRef}>{task.title}</h2>
      <div className="explorer-detail-badges">
        <Status tone={tone(task.status)}>{task.status}</Status>
        {task.priority && <span className="explorer-pill">{task.priority}</span>}
        <span className="explorer-pill delivery">delivery: {task.latestDeliveryStatus}</span>
      </div>
      <details className="explorer-ids"><summary>scoped identity</summary>
        <dl>
          <div><dt>Scoped id</dt><dd>{task.scopedId}</dd></div>
          <div><dt>Run label</dt><dd>{task.runLabel || '—'}</dd></div>
          <div><dt>PRD revision</dt><dd>r{task.prdRevision ?? '—'}</dd></div>
        </dl>
      </details>
      <section aria-label="Dependencies" className="explorer-detail-block">
        <h3>Dependencies</h3>
        {task.dependsOn.length
          ? <ul className="explorer-deps">{task.dependsOn.map((dep) => <li key={dep.scopedId}>{dep.scopedId}{dep.prdRevision != null ? ` @r${dep.prdRevision}` : ''}</li>)}</ul>
          : <p className="explorer-muted">No dependencies.</p>}
      </section>
      <section aria-label="Acceptance criteria" className="explorer-detail-block">
        <h3>Acceptance criteria</h3>
        <p>{task.acceptanceCriteriaCount} acceptance {task.acceptanceCriteriaCount === 1 ? 'criterion' : 'criteria'}</p>
      </section>
      <section aria-label="Verification" className="explorer-detail-block">
        <h3>Verification</h3>
        <p>{task.verificationSummary || 'No verification summary in this projection.'}</p>
      </section>
      <section aria-label="Delivery eligibility" className="explorer-detail-block">
        <h3>Delivery eligibility</h3>
        <ExplorerEligibility eligibility={eligibility} />
      </section>
    </section>
  }
  if (reading) {
    const prd = reading.prd
    return <section className="explorer-detail-pane" aria-label={`PRD ${prd ? prd.title : reading.prdId}`}>
      <div className="explorer-detail-topbar"><span className="crumb">PRD content / {prd?.redactionStatus || 'redacted'}</span><button className="explorer-close" aria-label="Close" onClick={onClose}>Close</button></div>
      <h2 className="explorer-detail-title" tabIndex={-1} ref={detailRef}>{prd ? prd.title : reading.prdId}</h2>
      {prd ? <>
        <dl className="explorer-prd-meta">
          <div><dt>Release</dt><dd>{prd.release || '—'}</dd></div>
          <div><dt>Revision</dt><dd>r{prd.revision ?? '—'}</dd></div>
          <div><dt>State status</dt><dd>{prd.status}</dd></div>
          <div><dt>Freshness</dt><dd>{freshnessLabel(prd.generatedAt)}</dd></div>
        </dl>
        {prd.truncated && <p className="explorer-muted">Showing a redacted, truncated projection{prd.totalBytes != null ? ` (${prd.totalBytes} bytes total)` : ''}.</p>}
        <pre className="explorer-prd-body">{prd.body || 'No PRD body in this projection.'}</pre>
      </> : <p className="explorer-degraded">{reading.prdError || 'PRD content is unavailable.'}</p>}
    </section>
  }
  return <section className="explorer-detail-pane empty" aria-label="Nothing selected">
    <h2 className="explorer-detail-title">Open a PRD, then a task</h2>
    <p className="explorer-muted">Select a project and open a PRD by its id to read its content and browse its plan and tasks. Titles and lineage lead; the scoped id keeps two PRDs' tasks distinct.</p>
  </section>
}

function ExplorerView({ data, append }) {
  const projects = data.projects || []
  const [selectedProjectId, setSelectedProjectId] = useState(projects[0]?.id || '')
  const [prdInput, setPrdInput] = useState('')
  const [entries, setEntries] = useState([])
  const [filter, setFilter] = useState('')
  const [selection, setSelection] = useState(null)
  const [reading, setReading] = useState(null)
  const [eligibility, setEligibility] = useState({ status: 'idle', value: null, message: null })
  const [announce, setAnnounce] = useState('')
  const detailRef = useRef(null)
  const railRef = useRef(null)
  // Latest-wins guard for the per-task eligibility fetch (a11y + suite #2),
  // mirroring the chat rail's listSeqRef: every fetch claims a monotonic ticket
  // and applies its result only if it is still the newest. Without it, opening
  // alpha:T001 (slow) then beta:T001 (fast) lets alpha's late resolve overwrite
  // beta's verdict — showing the WRONG eligibility under beta, the exact
  // T001-vs-T001 confusion this explorer exists to prevent. closeDetail also
  // bumps it so a stale in-flight fetch cannot repaint a closed pane.
  const eligibilitySeqRef = useRef(0)

  useEffect(() => { if (!selectedProjectId && projects[0]) setSelectedProjectId(projects[0].id) }, [projects, selectedProjectId])
  // Move focus to the opened detail heading so a view switch / detail open never
  // drops focus to <body> (a11y focus management). tabIndex=-1 makes the h2 a
  // programmatic focus target without adding it to the tab order.
  useEffect(() => { if (selection || reading) detailRef.current?.focus() }, [selection?.scopedId, reading?.key])

  const loadEligibility = async (entry, task) => {
    const seq = (eligibilitySeqRef.current += 1)
    setEligibility({ status: 'loading', value: null, message: null })
    try {
      const value = await fetchTaskEligibility(entry.projectId, entry.prdId, task.taskId)
      if (seq !== eligibilitySeqRef.current) return // a newer task open superseded this fetch
      setEligibility({ status: 'loaded', value: describeEligibility(value.eligibility), message: null })
    } catch (error) {
      if (seq !== eligibilitySeqRef.current) return // superseded fetch failed late; do not repaint
      setEligibility({ status: 'error', value: null, message: error.message })
    }
  }

  const openPrd = async () => {
    const project = projects.find((item) => item.id === selectedProjectId)
    const prdId = prdInput.trim()
    if (!project || !prdId) return
    const key = `${project.id}::${prdId}`
    setAnnounce(`Loading PRD ${prdId}…`)
    const [contentResult, tasksResult] = await Promise.allSettled([
      fetchPrdContent(project.id, prdId),
      fetchPrdTasks(project.id, prdId),
    ])
    const entry = {
      key, projectId: project.id, projectName: project.name, prdId,
      prd: contentResult.status === 'fulfilled' ? describePrdContent(contentResult.value.content) : null,
      prdError: contentResult.status === 'rejected' ? contentResult.reason.message : null,
      tasks: tasksResult.status === 'fulfilled' ? (tasksResult.value.tasks || []) : [],
      tasksError: tasksResult.status === 'rejected' ? tasksResult.reason.message : null,
    }
    setEntries((current) => [entry, ...current.filter((item) => item.key !== key)])
    setPrdInput('')
    // Announce the truthful outcome (a11y #6): when the PRD content loads but the
    // task projection fails, say so — never "…with 0 tasks", which reads as an
    // empty-but-healthy PRD and hides the failure (which is otherwise visual-only).
    if (entry.prd) {
      setAnnounce(entry.tasksError
        ? `Loaded PRD ${entry.prd.title}; tasks failed to load`
        : `Loaded PRD ${entry.prd.title} with ${entry.tasks.length} tasks`)
    } else { setAnnounce(entry.prdError || `PRD ${prdId} could not be loaded`); append?.(entry.prdError || `PRD ${prdId} could not be loaded`) }
  }

  const readPrd = (entry) => {
    setSelection(null)
    setReading(entry)
    setAnnounce(`Reading PRD ${entry.prd ? entry.prd.title : entry.prdId}`)
  }

  const openTask = (entry, task) => {
    setReading(null)
    setSelection({ scopedId: task.scopedId, entry, task })
    // Reflect the scoped identity in the URL so two PRDs' T001 are distinct in
    // state AND in the address bar (criterion 2), not merely on screen.
    if (task.scopedId) window.location.hash = `explorer/${task.scopedId}`
    setAnnounce(`Opened task ${task.scopedId}: ${task.title}`)
    loadEligibility(entry, task)
  }

  const closeDetail = () => {
    const invokedScopedId = selection?.scopedId || null
    const had = selection || reading
    setSelection(null)
    setReading(null)
    setEligibility({ status: 'idle', value: null, message: null })
    eligibilitySeqRef.current += 1 // drop any in-flight eligibility fetch (a11y #2)
    // Clear the write-only URL hash so it stops lying once the pane is closed
    // (a11y + suite #4): the hash reflects an OPEN task, so a closed detail must
    // not leave a stale `#explorer/<scoped id>` in the address bar.
    if (window.location.hash.startsWith('#explorer/')) window.location.hash = ''
    if (had) {
      setAnnounce('Closed detail')
      // Return focus to the invoking task row (a11y #9), not the first project
      // button in document order — a keyboard user deep in a second PRD is not
      // teleported to the top. Fall back to the first focusable rail control.
      const invoker = invokedScopedId && railRef.current?.querySelector(`[data-explorer-task="${invokedScopedId}"]`)
      const target = invoker || railRef.current?.querySelector('.explorer-open-prd, input, button')
      target?.focus()
    }
  }

  // Document-level Escape closes the detail while it is open (a11y #3), so it
  // works even after focus has left the pane (in PRD-read mode the pane has no
  // tabbable elements, so one Tab exits and a pane-scoped handler would go dead).
  useEffect(() => {
    if (!selection && !reading) return undefined
    const onDocKeyDown = (event) => { if (event.key === 'Escape') closeDetail() }
    document.addEventListener('keydown', onDocKeyDown)
    return () => document.removeEventListener('keydown', onDocKeyDown)
  }, [selection, reading])

  // Clear the URL hash when the Explorer view unmounts (a11y + suite #4): leaving
  // the Explorer route must not leave a stale `#explorer/<scoped id>` behind.
  useEffect(() => () => { if (window.location.hash.startsWith('#explorer/')) window.location.hash = '' }, [])

  // Announce the filter result in the live region on filter change (a11y #7):
  // filtering is named in the criterion-4 SR smoke but otherwise gives a
  // screen-reader user no feedback. An empty filter makes no announcement (it is
  // the resting state, and openPrd already owns the load announcement).
  useEffect(() => {
    const needle = filter.trim()
    if (!needle) return
    const total = entries.reduce(
      (sum, entry) => sum + filterDescribedTasks((entry.tasks || []).map(describeTaskReference), filter).length,
      0,
    )
    setAnnounce(total ? `Filter shows ${total} task${total === 1 ? '' : 's'}` : `No tasks match "${needle}"`)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fire on filter edits only
  }, [filter])

  return <main className="explorer" aria-label="Delivery explorer">
    <aside className="explorer-rail" ref={railRef}>
      <span className="crumb">Explorer / PRD → plan → task</span>
      <h1>Delivery explorer</h1>
      <p className="explorer-intro">Read-only Project → PRD → plan → task lineage from the redacted delivery projection.</p>
      <section aria-label="Projects" className="explorer-projects">
        <p className="explorer-section-title">Projects</p>
        {projects.length
          ? <div className="explorer-project-list">{projects.map((project) => <button key={project.id} className={`explorer-project ${selectedProjectId === project.id ? 'selected' : ''}`} aria-pressed={selectedProjectId === project.id} aria-label={`Select project ${project.name}`} onClick={() => setSelectedProjectId(project.id)}><b>{project.name}</b><small>{project.id}</small></button>)}</div>
          : <p className="explorer-muted">No projects yet. Create one from Delivery.</p>}
      </section>
      <form className="explorer-open" onSubmit={(event) => { event.preventDefault(); openPrd() }}>
        <label>Open a PRD by id<input value={prdInput} onChange={(event) => setPrdInput(event.target.value)} placeholder="e.g. release-alpha" disabled={!selectedProjectId} /></label>
        <button className="explorer-open-prd" type="submit" disabled={!selectedProjectId || !prdInput.trim()}>Open PRD</button>
        <small className="explorer-muted">PRD enumeration is not served by the projection; open a PRD by its id.</small>
      </form>
      <label className="explorer-filter">Filter tasks<input type="search" aria-label="Filter tasks" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="title, scoped id, or status" /></label>
      <section aria-label="Loaded PRDs" className="explorer-prds">
        {entries.length
          ? entries.map((entry) => <ExplorerPrdCard key={entry.key} entry={entry} filter={filter} selectedScopedId={selection?.scopedId || null} onReadPrd={readPrd} onOpenTask={openTask} />)
          : <p className="explorer-empty">No PRD opened yet. Choose a project and open a PRD by id.</p>}
      </section>
    </aside>
    <ExplorerDetail selection={selection} reading={reading} eligibility={eligibility} detailRef={detailRef} onClose={closeDetail} />
    <div className="explorer-live" role="status" aria-live="polite">{announce}</div>
  </main>
}

function App() {
  const [active, setActive] = useState('Chat'); const [data, setData] = useState(emptyData); const [notice, setNotice] = useState(''); const [selectedApprovalId, setSelectedApprovalId] = useState(null); const [newDeliveryOpen, setNewDeliveryOpen] = useState(false); const [newSessionOpen, setNewSessionOpen] = useState(false); const [startSession, setStartSession] = useState(null); const [guideOpen, setGuideOpen] = useState(false); const [profileOpen, setProfileOpen] = useState(false); const [notificationsOpen, setNotificationsOpen] = useState(false); const [notificationsRead, setNotificationsRead] = useState(false); const [deliverOpen, setDeliverOpen] = useState(false)
  const load = async () => { const value = await bootstrap(); setData({ ...emptyData, ...value, sandbox: { ...emptyData.sandbox, ...(value.sandbox || {}) }, voice: { ...emptyData.voice, ...(value.voice || {}) } }); return value }
  useEffect(() => { load().catch(() => setNotice('Workbench hub is unavailable; no local mock delivery is shown.')) }, [])
  const createDelivery = async (payload) => { try { const project = await createProject(payload); setData((current) => ({ ...current, projects: [project, ...current.projects] })); setNewDeliveryOpen(false); setNotice(`Created ${project.name}. Register its bridge locally before starting a run.`); await load() } catch { setNotice('Project could not be created. No bridge or run was started.') } }
  const createConcurrentSession = async (payload) => { try { const created = await createSession(payload); setData((current) => ({ ...current, sessions: [created.session, ...current.sessions], workflows: [created.workflow, ...current.workflows] })); setNewSessionOpen(false); setActive('Sessions'); setNotice(`Created ${created.session.title}. Start it only after its named worktree is configured on the bridge.`); await load() } catch { setNotice('Session could not be created. No bridge run was started.') } }
  const startConcurrentSession = async (workflowId, payload) => { try { const result = await startWorkflow(workflowId, payload); setStartSession(null); setActive('Delivery'); setNotice(`Started ${payload.task_id} through the local bridge. The workflow is traceable in this session.`); setData((current) => ({ ...current, runs: [result.run, ...current.runs] })); await load() } catch { setNotice('Workflow did not start. Check the project bridge, published skills, and named worktree configuration.') } }
  // The one-activation Deliver: start the ranked candidate through the REAL wired
  // POST /api/workflows/{id}/start, then route to the resulting run. Throws on
  // failure so the sheet keeps itself open and announces a truthful error (it
  // does NOT close or fabricate a started run).
  const deliverCandidate = async (workflowId, payload, signal) => {
    const result = await startWorkflow(workflowId, payload, { signal })
    setDeliverOpen(false); setActive('Delivery')
    setData((current) => ({ ...current, runs: [result.run, ...current.runs] }))
    setNotice(`Delivering ${payload.task_id} through the local bridge. Its run is ${result.run.id}.`)
    await load()
    return result.run
  }
  const addDirection = async (sessionId, text) => { const result = await addDirective(sessionId, text); if (result.recorded && result.event) { setData((current) => ({ ...current, directives: [...current.directives, result.event] })); setNotice('Direction recorded. It will be included only in the next bridge work packet for this session.') } else { setNotice(`Direction was not recorded (${result.outcome}). No future work packet was changed.`) } await load() }
  const context = useMemo(() => active === 'Delivery' ? 'Delivery cockpit' : `${active} view`, [active])
  const selectApproval = (approvalId) => { setSelectedApprovalId(approvalId); setActive('Delivery') }
  return <div className={`app-shell${active === 'Chat' ? ' chat-active' : ''}${active === 'Voice' ? ' voice-active' : ''}${active === 'Explorer' ? ' explorer-active' : ''}${active === 'Settings' ? ' settings-active' : ''}${active === 'Plugins' ? ' plugins-active' : ''}`}>
    <Rail active={active} setActive={setActive} onNewDelivery={() => setNewDeliveryOpen(true)} onProfile={() => setProfileOpen(!profileOpen)} />
    {profileOpen && <ProfileMenu data={data} onClose={() => setProfileOpen(false)} />}
    <div className="workspace"><header className="topbar"><span>{context}</span><div><Status tone={data.router_configured ? 'green' : 'amber'}>{data.router_configured ? 'router configured' : 'router not configured'}</Status><ModelHealthIndicator /><button className="help" aria-label="Help" onClick={() => setGuideOpen(true)}>?</button><button className="bell" aria-label="Notifications" aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(!notificationsOpen)}>♢</button></div></header>
      {notificationsOpen && <Notifications audit={data.audit || []} read={notificationsRead} onRead={() => setNotificationsRead(true)} />}
      {active === 'Chat'
        ? <div className="chat-grid"><ChatView append={setNotice} /></div>
        : active === 'Voice'
        ? <div className="voice-grid"><VoicePage data={data} append={setNotice} /></div>
        : active === 'Settings'
        ? <div className="settings-grid"><SettingsView data={data} append={setNotice} /><ConfigurationView data={data} append={setNotice} /></div>
        : active === 'Explorer'
        ? <div className="explorer-grid"><ExplorerView data={data} append={setNotice} /></div>
        : active === 'Plugins'
        ? <div className="pc-grid"><PluginCatalogView data={data} append={setNotice} /></div>
        : <div className="main-grid">
            {active === 'Delivery' ? <Delivery data={data} append={setNotice} onDirective={addDirection} onGuide={() => setGuideOpen(true)} onDeliverNext={() => setDeliverOpen(true)} /> : <WorkspaceView active={active} data={data} onNewSession={() => setNewSessionOpen(true)} onStartSession={(session, workflow) => setStartSession({ session, workflow })} append={setNotice} refresh={load} selectApproval={selectApproval} />}
            <Trace data={data} setActive={setActive} append={setNotice} refresh={load} selectedApprovalId={selectedApprovalId} clearApproval={() => setSelectedApprovalId(null)} />
          </div>}
      {notice && <div className="toast" role="status">{notice}<button aria-label="Dismiss notification" onClick={() => setNotice('')}>×</button></div>}
    </div>
    {newDeliveryOpen && <NewDelivery onClose={() => setNewDeliveryOpen(false)} onCreate={createDelivery} />}
    {newSessionOpen && <NewSession project={data.projects[0]} skills={data.skills.filter((skill) => skill.bridge_id === data.projects[0]?.bridge_id)} onClose={() => setNewSessionOpen(false)} onCreate={createConcurrentSession} />}
    {startSession && <StartSession session={startSession.session} workflow={startSession.workflow} onClose={() => setStartSession(null)} onStart={startConcurrentSession} />}
    {guideOpen && <Onboarding data={data} onClose={() => setGuideOpen(false)} setActive={setActive} onNewDelivery={() => setNewDeliveryOpen(true)} onNewSession={() => setNewSessionOpen(true)} />}
    {deliverOpen && <DeliverSheet project={data.projects[0]} workflows={data.workflows} sessions={data.sessions} runs={data.runs} onClose={() => setDeliverOpen(false)} onDeliver={deliverCandidate} />}
  </div>
}

export default App
