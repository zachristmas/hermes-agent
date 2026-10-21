import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $connection } from '@/store/session'

import { captureGatewayFileDownload, captureGatewayFilePreview } from './media'

beforeEach(() => {
  clearNotifications()
  // The window's focused connection must never leak into a captured owner.
  $connection.set({ connectionId: 'focused-host', mode: 'remote', profile: 'focused' } as never)
})

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  $connection.set(null)
  clearNotifications()
  vi.unstubAllGlobals()
})

const lastToast = () => $notifications.get()[0]

describe('captured gateway file download', () => {
  it('reports an unavailable desktop bridge', async () => {
    vi.stubGlobal('hermesDesktop', {})

    await expect(captureGatewayFileDownload()('/persisted/file.md', 'file.md')).resolves.toBeUndefined()

    expect(lastToast()).toMatchObject({ kind: 'error', title: 'Download failed' })
    expect(lastToast()?.message).toContain('Desktop file download bridge is unavailable')
  })

  it('rejects an absent stored path before invoking the bridge', async () => {
    const saveGatewayFile = vi.fn()
    vi.stubGlobal('hermesDesktop', { saveGatewayFile })

    await captureGatewayFileDownload()('  ', 'file.md')

    expect(saveGatewayFile).not.toHaveBeenCalled()
    expect(lastToast()?.message).toContain('Missing gateway file path')
  })

  it('confirms a completed save and stays quiet on cancel', async () => {
    const saveGatewayFile = vi.fn().mockResolvedValueOnce({ path: '/Users/me/Downloads/file.md', saved: true })
    vi.stubGlobal('hermesDesktop', { saveGatewayFile })
    const download = captureGatewayFileDownload()

    await download('/persisted/file.md', 'file.md')
    expect(lastToast()).toMatchObject({ kind: 'info', message: 'Saved' })

    clearNotifications()
    saveGatewayFile.mockResolvedValueOnce({ canceled: true, saved: false })
    await download('/persisted/file.md', 'file.md')
    expect($notifications.get()).toEqual([])
  })

  it('preserves legacy primary routing instead of inventing a default profile', async () => {
    const saveGatewayFile = vi.fn().mockResolvedValue({ saved: true })
    vi.stubGlobal('hermesDesktop', { saveGatewayFile })
    const download = captureGatewayFileDownload()
    setApiRequestConnection('remote')
    setApiRequestProfile('work')

    await download('/persisted/file.md', 'file.md')

    expect(saveGatewayFile).toHaveBeenCalledWith({ path: '/persisted/file.md', suggestedName: 'file.md' })
  })

  it('keeps explicit local ownership even after switching to a remote', async () => {
    const saveGatewayFile = vi.fn().mockResolvedValue({ canceled: true, saved: false })
    vi.stubGlobal('hermesDesktop', { saveGatewayFile })
    setApiRequestConnection('local')
    setApiRequestProfile('personal')
    const download = captureGatewayFileDownload()
    setApiRequestConnection('remote')
    setApiRequestProfile('work')

    await download('/persisted/file.md', 'file.md')

    expect(saveGatewayFile).toHaveBeenCalledWith({
      connectionId: 'local',
      profile: 'personal',
      path: '/persisted/file.md',
      suggestedName: 'file.md'
    })
  })
})

describe('captured gateway file preview', () => {
  it('reads through the pinned owner via the read-data-url contract', async () => {
    const api = vi.fn().mockResolvedValue({ dataUrl: 'data:text/plain;base64,aGk=' })
    vi.stubGlobal('hermesDesktop', { api })
    setApiRequestConnection('remote')
    setApiRequestProfile('work')
    const preview = captureGatewayFilePreview()
    setApiRequestConnection('other')
    setApiRequestProfile('elsewhere')

    await expect(preview('/persisted/file.md')).resolves.toBe('data:text/plain;base64,aGk=')
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        connectionId: 'remote',
        path: '/api/fs/read-data-url?path=%2Fpersisted%2Ffile.md',
        profile: 'work'
      })
    )
  })

  it('rejects empty and relative paths before any read', async () => {
    const api = vi.fn()
    vi.stubGlobal('hermesDesktop', { api })
    const preview = captureGatewayFilePreview()

    await expect(preview('  ')).rejects.toThrow('Missing gateway file path')
    await expect(preview('relative/file.md')).rejects.toThrow('Missing gateway file path')
    expect(api).not.toHaveBeenCalled()
  })

  it('rejects a result that is not a data URL', async () => {
    vi.stubGlobal('hermesDesktop', { api: vi.fn().mockResolvedValue({ dataUrl: '' }) })
    setApiRequestConnection('remote')

    await expect(captureGatewayFilePreview()('/persisted/file.md')).rejects.toThrow('Preview unavailable')
  })
})
