import { describe, expect, it } from 'vitest'
import { formatBytes, jobRef, pad, trackProgressLabel } from './format'
import { checkPlaylistUrl } from './validation'

const PID = '37i9dQZF1DXcBWIGoYBM5M'

describe('checkPlaylistUrl', () => {
  it.each([
    `https://open.spotify.com/playlist/${PID}`,
    `https://open.spotify.com/playlist/${PID}?si=abc123`,
    `https://open.spotify.com/intl-de/playlist/${PID}`,
    `  open.spotify.com/playlist/${PID}/ `,
  ])('accepts %s', (url) => {
    const r = checkPlaylistUrl(url)
    expect(r.ok).toBe(true)
    if (r.ok) expect(r.playlistId).toBe(PID)
  })

  it.each([
    '',
    'hello',
    `http://open.spotify.com/playlist/${PID}`,
    `https://open.spotify.com.evil.com/playlist/${PID}`,
    `https://user@open.spotify.com/playlist/${PID}`,
    `https://open.spotify.com:443/playlist/${PID}`,
    `https://open.spotify.com/playlist/short`,
    `spotify:playlist:${PID}`,
  ])('rejects %s', (url) => {
    expect(checkPlaylistUrl(url).ok).toBe(false)
  })

  it('explains non-playlist spotify links', () => {
    const r = checkPlaylistUrl(`https://open.spotify.com/album/${PID}`)
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.message).toContain('not a playlist')
  })
})

describe('format helpers', () => {
  it('pads and labels progress', () => {
    expect(pad(3)).toBe('03')
    expect(pad(7, 3)).toBe('007')
    expect(trackProgressLabel(12, 35)).toBe('12 of 35 tracks processed')
    expect(trackProgressLabel(0, 1)).toBe('0 of 1 track processed')
    expect(trackProgressLabel(0, 0)).toBe('reading playlist')
  })

  it('formats bytes', () => {
    expect(formatBytes(512)).toBe('512 b')
    expect(formatBytes(1536)).toBe('1.5 kb')
    expect(formatBytes(150 * 1024 * 1024)).toBe('150.0 mb')
    expect(formatBytes(null)).toBe('—')
  })

  it('builds a job reference', () => {
    expect(jobRef('3f2a9c0000000000000000000000abcd')).toBe('3f2a-9c')
  })
})
