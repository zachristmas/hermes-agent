import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness drives the host's request scope, as a connection switch does.
// eslint-disable-next-line no-restricted-imports
import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'
// Test harness reads the host's toast stack.
// eslint-disable-next-line no-restricted-imports
import { $notifications, clearNotifications } from '@/store/notifications'

import { bindApi, taskKey } from './api'
import { parseHerdrRef, TaskDrawer } from './drawer'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail, KanbanTaskRef } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const legacyDetail: Omit<KanbanTaskDetail, 'attachments'> = {
  task: { id: 't_example', title: 'Example task', body: 'Keep this description readable.', status: 'todo' },
  comments: [{ id: 1, author: 'test', body: 'Keep this comment readable.', created_at: 0 }],
  events: [],
  links: { parents: [], children: [] },
  runs: []
}

let detail: object
let refs: KanbanTaskRef[]
let refsEndpointMissing: boolean
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string, options?: PluginRestOptions): Promise<unknown> => {
  if (path === '/tasks/t_example/attachments' && options?.method === 'POST') {
    detail = { ...legacyDetail, attachments: [{ id: 1, filename: options.upload?.filename }] }

    return { ok: true }
  }

  if (path.startsWith('/tasks/t_example/comments') && options?.method === 'POST') {
    return { ok: true }
  }

  if (path === '/tasks/t_example') {
    return detail
  }

  if (path === '/tasks/t_example/refs') {
    if (refsEndpointMissing) {
      throw new Error('404: no such route')
    }

    return { refs }
  }

  if (path.startsWith('/tasks/t_example/log?')) {
    return { exists: false, content: '', size_bytes: 0, truncated: false }
  }

  if (path === '/profiles') {
    return { profiles: [] }
  }

  if (path === '/orchestration') {
    return { default_assignee: '' }
  }

  throw new Error(`Unexpected REST request: ${path}`)
})

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
  refs = []
  refsEndpointMissing = false
})

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  clearNotifications()
  vi.unstubAllGlobals()
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  vi.clearAllMocks()
})

function openDrawer() {
  return render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={vi.fn()} onOpen={vi.fn()} />
    </QueryClientProvider>
  )
}

describe('task attachment compatibility', () => {
  it('downloads the persisted attachment through its original remote owner', async () => {
    const save = vi.fn().mockResolvedValue({ saved: true })
    vi.stubGlobal('hermesDesktop', { saveGatewayFile: save })
    setApiRequestConnection('remote-owner')
    setApiRequestProfile('research')
    detail = {
      ...legacyDetail,
      attachments: [{ id: 42, filename: 'report.md', stored_path: '/persisted/attachments/report.md' }]
    }
    openDrawer()
    const download = await screen.findByRole('button', { name: 'Download report.md' })
    setApiRequestConnection('other-host')
    setApiRequestProfile('other-profile')
    fireEvent.click(download)
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({
        connectionId: 'remote-owner',
        profile: 'research',
        path: '/persisted/attachments/report.md',
        suggestedName: 'report.md'
      })
    )
    await waitFor(() => expect($notifications.get()[0]).toMatchObject({ kind: 'info', message: 'Saved' }))
  })

  it('keeps a local-backend attachment on this computer after switching to a remote', async () => {
    const save = vi.fn().mockResolvedValue({ saved: true })
    vi.stubGlobal('hermesDesktop', { saveGatewayFile: save })
    setApiRequestConnection('local')
    detail = {
      ...legacyDetail,
      attachments: [{ id: 7, filename: 'notes.txt', stored_path: '/home/me/.hermes/kanban/notes.txt' }]
    }
    openDrawer()
    const download = await screen.findByRole('button', { name: 'Download notes.txt' })
    setApiRequestConnection('remote-host')
    fireEvent.click(download)
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({
        connectionId: 'local',
        path: '/home/me/.hermes/kanban/notes.txt',
        suggestedName: 'notes.txt'
      })
    )
  })

  it('previews a Markdown attachment inline through its original owner', async () => {
    const api = vi.fn().mockResolvedValue({ dataUrl: `data:text/markdown;base64,${btoa('# Plan\n\nship it')}` })
    vi.stubGlobal('hermesDesktop', { api })
    setApiRequestConnection('remote-owner')
    setApiRequestProfile('cs')
    detail = {
      ...legacyDetail,
      attachments: [{ id: 5, filename: 'plan.md', stored_path: '/persisted/attachments/plan.md' }]
    }
    openDrawer()
    const toggle = await screen.findByRole('button', { name: 'Preview plan.md' })
    setApiRequestConnection('other-host')
    fireEvent.click(toggle)
    expect(await screen.findByText('ship it')).toBeTruthy()
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        connectionId: 'remote-owner',
        path: '/api/fs/read-data-url?path=%2Fpersisted%2Fattachments%2Fplan.md',
        profile: 'cs'
      })
    )
    expect(screen.getByRole('button', { name: 'Hide preview of plan.md' })).toBeTruthy()
  })

  it('previews a plain-text attachment verbatim and reports a failed read', async () => {
    const api = vi
      .fn()
      .mockResolvedValueOnce({ dataUrl: `data:text/plain;base64,${btoa('line one\n<b>raw</b>')}` })
      .mockRejectedValueOnce(new Error('File not found'))

    vi.stubGlobal('hermesDesktop', { api })
    setApiRequestConnection('remote-owner')
    detail = {
      ...legacyDetail,
      attachments: [
        { id: 1, filename: 'notes.txt', stored_path: '/persisted/notes.txt' },
        { id: 2, filename: 'gone.log', stored_path: '/persisted/gone.log' }
      ]
    }
    openDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Preview notes.txt' }))
    expect(await screen.findByText(/<b>raw<\/b>/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Preview gone.log' }))
    expect(await screen.findByText('Preview unavailable')).toBeTruthy()
  })

  it('offers no preview for unsupported types, missing paths, or oversized files', async () => {
    detail = {
      ...legacyDetail,
      attachments: [
        { id: 1, filename: 'pic.png', stored_path: '/persisted/pic.png' },
        { id: 2, filename: 'old.md' },
        { id: 3, filename: 'huge.txt', stored_path: '/persisted/huge.txt', size: 50 * 1024 * 1024 }
      ]
    }
    openDrawer()
    await screen.findByRole('button', { name: 'Download pic.png' })
    expect(screen.queryByRole('button', { name: /^Preview / })).toBeNull()
  })

  it('disables downloads with no persisted path instead of guessing a workspace path', async () => {
    detail = { ...legacyDetail, attachments: [{ id: 1, filename: 'gone.md' }] }
    openDrawer()
    const button = await screen.findByRole('button', { name: 'Download gone.md' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
  })

  it('reports a failed download and allows retry', async () => {
    const save = vi.fn().mockRejectedValue(new Error('File not found'))
    vi.stubGlobal('hermesDesktop', { saveGatewayFile: save })
    detail = { ...legacyDetail, attachments: [{ id: 1, filename: 'gone.md', stored_path: '/persisted/gone.md' }] }
    openDrawer()
    const button = await screen.findByRole('button', { name: 'Download gone.md' })
    fireEvent.click(button)
    await waitFor(() =>
      expect($notifications.get()[0]).toMatchObject({
        kind: 'error',
        message: 'File not found',
        title: 'Download failed'
      })
    )
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    save.mockResolvedValueOnce({ saved: true })
    fireEvent.click(button)
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
  })

  it('disables a pending download and treats save-dialog cancellation quietly', async () => {
    let finish!: (value: { saved: boolean; canceled: boolean }) => void

    const save = vi.fn(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )

    vi.stubGlobal('hermesDesktop', { saveGatewayFile: save })
    detail = { ...legacyDetail, attachments: [{ id: 1, filename: 'report.md', stored_path: '/persisted/report.md' }] }
    openDrawer()
    const button = await screen.findByRole('button', { name: 'Download report.md' })
    fireEvent.click(button)
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(true))
    fireEvent.click(button)
    expect(save).toHaveBeenCalledOnce()
    await act(async () => finish({ saved: false, canceled: true }))
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    expect($notifications.get()).toEqual([])
  })

  it.each([{}, { attachments: null }])(
    'keeps older task details usable without attachment controls (%j)',
    async extra => {
      detail = { ...legacyDetail, ...extra }
      openDrawer()

      expect(await screen.findByRole('heading', { name: legacyDetail.task.title })).toBeTruthy()
      expect(screen.getByText(legacyDetail.task.body!)).toBeTruthy()
      expect(screen.getByText(legacyDetail.comments[0].body)).toBeTruthy()
      expect(screen.queryByRole('button', { name: en.uploadAttachment })).toBeNull()
      expect(screen.queryByText(en.noAttachments)).toBeNull()

      // A later backend response restores the capability without remounting.
      detail = { ...legacyDetail, attachments: [] }
      await act(() => client.invalidateQueries({ queryKey: taskKey('local', '', legacyDetail.task.id) }))
      expect(await screen.findByRole('button', { name: en.uploadAttachment })).toBeTruthy()
      expect(screen.getByText(en.noAttachments)).toBeTruthy()
    }
  )

  it('keeps upload and attachment rendering working for a supported empty list', async () => {
    detail = { ...legacyDetail, attachments: [] }
    openDrawer()
    const upload = await screen.findByRole('button', { name: en.uploadAttachment })
    expect(screen.getByText(en.noAttachments)).toBeTruthy()

    // The modal portals out of the render container; query inside the dialog.
    const input = screen.getByRole('dialog').querySelector<HTMLInputElement>('input[type="file"]')!
    const click = vi.spyOn(input, 'click')
    fireEvent.click(upload)
    expect(click).toHaveBeenCalledOnce()

    const file = new File(['example'], 'example.txt', { type: 'text/plain' })
    const bytes = new ArrayBuffer(7)
    // jsdom's File lacks arrayBuffer; the upload still uses the real REST adapter.
    Object.defineProperty(file, 'arrayBuffer', { value: async () => bytes })
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/tasks/t_example/attachments', {
        method: 'POST',
        upload: { filename: file.name, contentType: file.type, bytes }
      })
    )
    expect(await screen.findByText(file.name)).toBeTruthy()
    expect(screen.queryByText(en.noAttachments)).toBeNull()
  })
})

describe('task modal dialog', () => {
  it('is a modal dialog named by the task title that Esc dismisses', async () => {
    detail = { ...legacyDetail, attachments: [] }
    const onClose = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={onClose} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    const dialog = await screen.findByRole('dialog', { name: legacyDetail.task.title })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('posts a comment from the named icon action in the field', async () => {
    detail = { ...legacyDetail, attachments: [] }
    openDrawer()

    const send = await screen.findByRole('button', { name: en.comment })
    expect((send as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText(en.addComment), { target: { value: 'looks good' } })
    fireEvent.click(send)

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith(
        expect.stringMatching(/^\/tasks\/t_example\/comments/),
        expect.objectContaining({ method: 'POST', body: expect.objectContaining({ body: 'looks good' }) })
      )
    )
  })

  it('shows the workspace path as its own value, not prefixed with the raw kind', async () => {
    const path = '/Users/example/.hermes/kanban/workspaces/a_very_long_directory_name_that_must_wrap'
    detail = {
      ...legacyDetail,
      attachments: [],
      task: { ...legacyDetail.task, workspace_kind: 'dir', workspace_path: path }
    }
    openDrawer()

    expect(await screen.findByText(path)).toBeTruthy()
    expect(screen.queryByText(/dir:/)).toBeNull()
  })

  it('renders description and comments as markdown, not raw source', async () => {
    detail = {
      ...legacyDetail,
      attachments: [],
      task: { ...legacyDetail.task, body: '**Goal:** ship it' },
      comments: [{ id: 1, author: 'test', body: 'run `npm test`', created_at: 0 }]
    }
    openDrawer()

    // Formatted runs become their own nodes; the raw markers are gone.
    expect(await screen.findByText('Goal:')).toBeTruthy()
    expect(screen.getByText('npm test')).toBeTruthy()
    expect(screen.queryByText(/\*\*|`/)).toBeNull()
  })

  it('does not repeat the active feed tab as a heading above the tab strip', async () => {
    detail = {
      ...legacyDetail,
      attachments: [],
      events: [{ id: 1, kind: 'created', payload: null, created_at: 0 }]
    }
    openDrawer()

    const commentsLabel = en.comments(legacyDetail.comments.length)
    expect(await screen.findByRole('button', { name: commentsLabel, pressed: true })).toBeTruthy()
    expect(screen.getAllByText(commentsLabel)).toHaveLength(1)
  })
})

describe('dependency chips resolve titles', () => {
  const linkedDetail = {
    ...legacyDetail,
    attachments: [] as [],
    links: { parents: ['t_parent'], children: ['t_child'] },
    link_tasks: [
      { id: 't_parent', title: 'Parent title', status: 'todo' },
      { id: 't_child', title: 'Child title', status: 'running' }
    ]
  }

  it('renders linked task titles, not raw ids, and opens on click', async () => {
    detail = linkedDetail
    const onOpen = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={vi.fn()} onOpen={onOpen} />
      </QueryClientProvider>
    )

    expect(await screen.findByRole('heading', { name: legacyDetail.task.title })).toBeTruthy()
    expect(screen.getByText('Parent title')).toBeTruthy()
    expect(screen.getByText('Child title')).toBeTruthy()
    expect(screen.queryByText('parent')).toBeNull()

    fireEvent.click(screen.getByText('Parent title'))
    expect(onOpen).toHaveBeenCalledWith('t_parent')
  })

  it('falls back to short ids when the backend omits link_tasks', async () => {
    const { link_tasks: _omit, ...withoutTitles } = linkedDetail
    detail = withoutTitles
    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    expect(await screen.findByText('parent')).toBeTruthy()
    expect(screen.getByText('child')).toBeTruthy()
  })
})

// #124391: block_kind / block_recurrences / consecutive_failures /
// last_failure_error arrive on every payload — the drawer must show them,
// and a blocked task's diagnostic unblock action must PATCH the task ready.
describe('blocked task detail', () => {
  const blockedDetail = {
    ...legacyDetail,
    attachments: [] as [],
    task: {
      ...legacyDetail.task,
      status: 'blocked',
      block_kind: 'needs_input',
      block_recurrences: 2,
      consecutive_failures: 3,
      last_failure_error: 'RuntimeError: boom',
      diagnostics: [
        {
          kind: 'stuck_in_blocked',
          severity: 'warning',
          title: 'Parked in blocked',
          detail: 'This task has been blocked for a while.',
          actions: [{ kind: 'unblock', label: 'Unblock task' }],
          count: 1,
          last_seen_at: 0,
          data: {}
        }
      ]
    }
  }

  it('renders the block kind, recurrences, failures and last error the API returns', async () => {
    detail = blockedDetail
    openDrawer()

    expect(await screen.findByText('needs_input')).toBeTruthy()
    expect(screen.getByText('×2')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('RuntimeError: boom')).toBeTruthy()
  })

  it('hides block detail on legacy payloads and recovered tasks', async () => {
    // Legacy payload: none of the four fields.
    detail = { ...legacyDetail, attachments: [] }
    openDrawer()
    await screen.findByRole('heading', { name: legacyDetail.task.title })
    expect(screen.queryByText(en.blockReason)).toBeNull()
    expect(screen.queryByText(en.consecutiveFailures)).toBeNull()
    expect(screen.queryByText(en.lastFailureError)).toBeNull()
    cleanup()
  })

  it('does not present a retained block kind as current after recovery', async () => {
    // block_kind is retained across unblock — present but the task is no
    // longer blocked, so it must not read as a current block reason.
    detail = {
      ...blockedDetail,
      task: { ...blockedDetail.task, status: 'todo', consecutive_failures: 0, last_failure_error: null }
    }
    openDrawer()
    await screen.findByRole('heading', { name: blockedDetail.task.title })
    expect(screen.queryByText(en.blockReason)).toBeNull()
    expect(screen.queryByText('needs_input')).toBeNull()
  })

  it('fires the diagnostic unblock action as a PATCH to ready', async () => {
    detail = blockedDetail
    openDrawer()

    fireEvent.click(await screen.findByRole('button', { name: 'Unblock task' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith(
        '/tasks/t_example',
        expect.objectContaining({ method: 'PATCH', body: { status: 'ready' } })
      )
    )
  })
})

// The ref row the direct-Herdr bridge writes (kanban_direct_herdr_link
// persist_executor): ref = host/session/workspace/tab/pane, note names the
// agent, its native session (or "not reported"), and the run fence.
const herdrRef: KanbanTaskRef = {
  id: 31,
  task_id: 't_example',
  ref_type: 'herdr',
  ref: 'zchristmas-mbp/default/w1R/w1R:t35/w1R:p3A',
  host: 'zchristmas-mbp',
  note: 'agent clark; native session 8f2c91ab; run t_da6c6cae/run-171',
  created_at: 0
}

describe('herdr pane section', () => {
  it('renders a herdr ref as its five segments plus agent, run, and native-session state', async () => {
    refs = [herdrRef]
    openDrawer()

    await screen.findByTestId('kanban-herdr-panes')
    expect(screen.getByTestId('kanban-herdr-ref').textContent).toContain(
      '#31 zchristmas-mbp/default/w1R/w1R:t35/w1R:p3A'
    )
    expect(screen.getByTestId('kanban-herdr-host').textContent).toContain('zchristmas-mbp')
    expect(screen.getByTestId('kanban-herdr-session').textContent).toContain('default')
    expect(screen.getByTestId('kanban-herdr-workspace').textContent).toContain('w1R')
    expect(screen.getByTestId('kanban-herdr-tab').textContent).toContain('w1R:t35')
    expect(screen.getByTestId('kanban-herdr-pane-id').textContent).toContain('w1R:p3A')
    expect(screen.getByTestId('kanban-herdr-agent').textContent).toContain('clark')
    expect(screen.getByTestId('kanban-herdr-run').textContent).toContain('t_da6c6cae/run-171')
    expect(screen.getByTestId('kanban-herdr-native').textContent).toContain(en.herdrReported)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
  })

  it('marks a sessionless harness as not reported instead of showing a session id', async () => {
    refs = [{ ...herdrRef, note: 'agent rpi; native session not reported; run -' }]
    openDrawer()

    await screen.findByTestId('kanban-herdr-panes')
    expect(screen.getByTestId('kanban-herdr-native').textContent).toContain(en.herdrNotReported)
    // The agent line stays; the run line collapses on its "-" placeholder.
    expect(screen.getByTestId('kanban-herdr-agent').textContent).toContain('rpi')
    expect(screen.queryByTestId('kanban-herdr-run')).toBeNull()
  })

  it('hides entirely when the task has no herdr ref', async () => {
    refs = []
    openDrawer()

    expect(await screen.findByRole('heading', { name: legacyDetail.task.title })).toBeTruthy()
    expect(screen.queryByTestId('kanban-herdr-panes')).toBeNull()
  })

  it('renders one block per herdr ref and never shows other ref kinds', async () => {
    refs = [
      herdrRef,
      { id: 30, task_id: 't_example', ref_type: 'branch', ref: 'feature/drawer', created_at: 0 },
      {
        id: 32,
        task_id: 't_example',
        ref_type: 'herdr',
        ref: 'tolaria-win/default/w2R/w2R:t11/w2R:p9F',
        host: 'tolaria-win',
        note: 'agent axiom; native session b71c; run t_da6c6cae/run-172',
        created_at: 1
      }
    ]
    openDrawer()

    await screen.findByTestId('kanban-herdr-panes')
    const panes = screen.getAllByTestId('kanban-herdr-pane')
    expect(panes).toHaveLength(2)
    // Each block carries its own parsed fields — first pane's host is the Mac,
    // the branch ref (and its id) never renders.
    expect(panes[0].querySelector('[data-testid="kanban-herdr-ref"]')!.textContent).toContain('#31')
    expect(panes[0].querySelector('[data-testid="kanban-herdr-host"]')!.textContent).toContain('zchristmas-mbp')
    expect(panes[1].querySelector('[data-testid="kanban-herdr-ref"]')!.textContent).toContain('#32')
    expect(panes[1].querySelector('[data-testid="kanban-herdr-host"]')!.textContent).toContain('tolaria-win')
    expect(screen.queryByText('feature/drawer')).toBeNull()
    expect(screen.queryByText('#30')).toBeNull()
  })

  it('recovers from a backend without the refs endpoint (404) by hiding the section', async () => {
    refsEndpointMissing = true
    openDrawer()

    expect(await screen.findByRole('heading', { name: legacyDetail.task.title })).toBeTruthy()
    expect(screen.queryByTestId('kanban-herdr-panes')).toBeNull()
  })
})

describe('parseHerdrRef', () => {
  it('parses the pane path and the note into fields', () => {
    const pane = parseHerdrRef(herdrRef)

    expect(pane).toEqual({
      agent: 'clark',
      fence: 't_da6c6cae/run-171',
      host: 'zchristmas-mbp',
      id: 31,
      nativeSession: '8f2c91ab',
      pane: 'w1R:p3A',
      ref: 'zchristmas-mbp/default/w1R/w1R:t35/w1R:p3A',
      reported: true,
      session: 'default',
      tab: 'w1R:t35',
      workspace: 'w1R'
    })
  })

  it('rejects non-herdr rows and refs that are not the five-segment pane path', () => {
    expect(parseHerdrRef({ ...herdrRef, ref_type: 'branch' })).toBeNull()
    expect(parseHerdrRef({ ...herdrRef, ref: null })).toBeNull()
    expect(parseHerdrRef({ ...herdrRef, ref: 'zchristmas-mbp/default/w1R' })).toBeNull()
    expect(parseHerdrRef({ ...herdrRef, note: null })).toMatchObject({
      agent: null,
      fence: null,
      nativeSession: null,
      reported: false
    })
  })
})
