// Client-side pre-check that mirrors the server's rules (backend/app/spotify.py).
// The server re-validates everything; this only gives instant feedback.

const PLAYLIST_PATH = /^\/(?:intl-[a-z]{2}(?:-[a-z]{2,4})?\/)?playlist\/([A-Za-z0-9]{22})\/?$/
const OTHER_SPOTIFY_KIND = /^\/(?:intl-[^/]+\/)?(album|track|artist|show|episode|user)\//

export type UrlCheck = { ok: true; playlistId: string; url: string } | { ok: false; message: string }

export const INVALID_MESSAGE = 'invalid link. expected https://open.spotify.com/playlist/ followed by a playlist id.'

export function checkPlaylistUrl(raw: string): UrlCheck {
  let text = raw.trim()
  if (!text) return { ok: false, message: 'paste a spotify playlist link first.' }
  if (text.length > 512 || /\s/.test(text)) return { ok: false, message: INVALID_MESSAGE }
  if (text.toLowerCase().startsWith('open.spotify.com/')) text = `https://${text}`

  let parsed: URL
  try {
    parsed = new URL(text)
  } catch {
    return { ok: false, message: INVALID_MESSAGE }
  }
  // Reject explicit ports even when they equal the default (URL normalises :443 away).
  const explicitPort = /^https:\/\/[^/]*:\d*(\/|$)/i.test(text)
  if (
    parsed.protocol !== 'https:' ||
    parsed.hostname.toLowerCase() !== 'open.spotify.com' ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    explicitPort
  ) {
    return { ok: false, message: INVALID_MESSAGE }
  }
  const match = PLAYLIST_PATH.exec(parsed.pathname)
  if (!match?.[1]) {
    if (OTHER_SPOTIFY_KIND.test(parsed.pathname)) {
      return { ok: false, message: 'that is a spotify link, but not a playlist. only playlist links are supported.' }
    }
    return { ok: false, message: INVALID_MESSAGE }
  }
  return { ok: true, playlistId: match[1], url: text }
}
