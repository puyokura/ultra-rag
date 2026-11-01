/**
 * Test harness: drives the ultra-RAG dsh plugin with a simulated Cordis host.
 * It is not a substitute for a real dsh boot; the Python test labels it as a
 * plugin-level integration test.
 */
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.env.ULTRA_RAG_REPO
const home = process.env.ULTRA_RAG_HOME
const python = process.env.ULTRA_RAG_PYTHON
const pluginPath = path.join(root, 'rag', 'dsh-plugin', 'index.mjs')

const { apply, name } = await import(pathToFileURL(pluginPath).href)

const handlers = {}
const warnings = []
const ctx = {
  on(event, handler) {
    handlers[event] = handler
  },
  logger: {
    warn(message) {
      warnings.push(String(message))
    },
  },
}

apply(ctx, { home, python, timeoutMs: 15000 })

function textMessage(id, text) {
  return {
    id,
    role: 'user',
    source: { kind: 'user' },
    content: [{ type: 'text', text }],
  }
}

const session = {
  id: 'sess_plugin_0001',
  header: { cwd: root, delegationDepth: 0, origin: undefined },
}

const userMessage = textMessage('msg-user-1', 'この案件では必ず実プロセスで検証する方針です。')
if (typeof handlers['agent/pre-step'] !== 'function') {
  throw new Error('agent/pre-step handler was not registered')
}
const decision = await handlers['agent/pre-step'](
  { agent: { session }, messages: [userMessage], turn: 1, step: 1, signal: undefined },
  async () => ({ kind: 'enter', messages: [userMessage] }),
)
const injected = decision.messages.filter((message) => message?.source?.kind === name)
if (decision.kind !== 'enter' || injected.length !== 1) {
  throw new Error(`context injection failed: kind=${decision.kind} injected=${injected.length}`)
}
const injectedText = injected[0].content.map((block) => block.text).join('\n')
if (!injectedText.includes('必ず実プロセスで検証する')) {
  throw new Error('injected context does not contain the stored requirement')
}
if (!decision.messages[decision.messages.length - 1].source.plugin) {
  throw new Error('context message was not appended after the user message')
}

const nextUser = textMessage('msg-user-1b', '次の依頼です。')
const secondDecision = await handlers['agent/pre-step'](
  { agent: { session }, messages: [nextUser], turn: 2, step: 1, signal: undefined },
  async () => ({ kind: 'enter', messages: [...decision.messages, nextUser] }),
)
const secondOwn = secondDecision.messages.filter((message) => message?.source?.kind === name)
if (secondOwn.length !== 1) {
  throw new Error(`context block was not replaced: ${secondOwn.length} ultra-RAG messages`)
}

const sessionHandler = handlers['session/event']
if (typeof sessionHandler !== 'function') {
  throw new Error('session/event handler was not registered')
}
sessionHandler(session, { type: 'turn/start', data: { turn: 1 } })
sessionHandler(session, {
  type: 'user/message',
  data: {
    id: 'msg-user-2',
    role: 'user',
    source: { kind: 'user' },
    content: [{ type: 'text', text: '設計より実測を優先する方針で進めます。' }],
  },
})
sessionHandler(session, {
  type: 'user/message',
  data: {
    id: 'msg-user-2b',
    role: 'user',
    source: { kind: 'user' },
    content: [{ type: 'text', text: '追加の指示です。' }],
  },
})
sessionHandler(session, {
  type: 'user/message',
  data: {
    id: 'msg-user-2c',
    role: 'user',
    source: { kind: 'user' },
    content: [{ type: 'text', text: '以後、必ず実測することを覚えて。' }],
  },
})
sessionHandler(session, {
  type: 'assistant/message',
  data: {
    message: {
      id: 'msg-assistant-2',
      role: 'assistant',
      source: { kind: 'model', provider: 'test', model: 'test' },
      content: [{ type: 'text', text: '了解しました。以後、必ず実測します。' }],
    },
  },
})
sessionHandler(session, { type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } })

await new Promise((resolve) => setTimeout(resolve, 2000))

// #17: a memory change in the same turn must refresh the injected block.
await new Promise((resolve) => setTimeout(resolve, 2000))
const beforeSummary = secondDecision.messages.find((message) => message?.source?.kind === name)?.source?.summary
const refreshed = await handlers['agent/pre-step'](
  { agent: { session }, messages: secondDecision.messages, turn: 2, step: 2, signal: undefined },
  async () => ({ kind: 'enter', messages: secondDecision.messages }),
)
const refreshedOwn = refreshed.messages.filter((message) => message?.source?.kind === name)
if (refreshedOwn.length !== 1) {
  throw new Error(`cache refresh duplicated the context block: ${refreshedOwn.length}`)
}
if (refreshedOwn[0].source.summary === beforeSummary) {
  throw new Error('the injected block was not refreshed after a memory update')
}

// #16: a hook that cannot start must surface a warning instead of failing silently.
const failureWarnings = []
const failureHandlers = {}
const failureCtx = {
  on(event, handler) {
    failureHandlers[event] = handler
  },
  logger: {
    warn(message) {
      failureWarnings.push(String(message))
    },
  },
}
apply(failureCtx, {
  home,
  python: process.env.ULTRA_RAG_BAD_PYTHON || 'ultra-rag-missing-python-command',
  timeoutMs: 5000,
})
const failureSession = { id: 'sess_plugin_failure', header: { cwd: root, delegationDepth: 0 } }
failureHandlers['session/event'](failureSession, { type: 'turn/start', data: { turn: 1 } })
failureHandlers['session/event'](failureSession, {
  type: 'user/message',
  data: { id: 'fail-user-1', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '失敗検知の確認です。' }] },
})
failureHandlers['session/event'](failureSession, { type: 'turn/end', data: { turn: 1 } })
await new Promise((resolve) => setTimeout(resolve, 1500))

console.log(
  JSON.stringify({
    ok: true,
    name,
    injected: injectedText,
    summary: injected[0].source.summary,
    warnings,
    failureWarnings,
  }),
)
