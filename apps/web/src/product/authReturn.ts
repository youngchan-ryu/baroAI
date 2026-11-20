export function safeReturnPath(value: string | null | undefined, role: string): string {
  const fallback = role === 'dispatcher' ? '/admin/queue' : role === 'worker' ? '/work' : '/report'
  if (role === 'reporter' && value?.startsWith('/report?') && !value.includes('#') && !value.includes('\\')) {
    const params = new URLSearchParams(value.slice('/report?'.length))
    const loc = params.get('loc')
    if (loc && loc.length <= 100) {
      const room = params.get('room')
      const query = new URLSearchParams({ loc })
      if (params.size === 1) return `/report?${query}`
      if (params.size === 2 && room && /^[0-9A-Za-z가-힣-]{1,20}$/.test(room)) {
        query.set('room', room)
        return `/report?${query}`
      }
    }
  }
  if (!value || value.includes('\\') || value.includes('%') || value.includes('?') || value.includes('#')) return fallback
  if (role === 'worker') return value === '/work' || /^\/work\/[a-zA-Z0-9-]+$/.test(value) ? value : fallback
  if (role === 'dispatcher') return ['/admin/queue', '/admin/planning', '/admin/settings', '/admin/export', '/admin/staff-applications'].includes(value) || /^\/admin\/planning\/[a-zA-Z0-9-]+(?:\/insertions)?$/.test(value) || /^\/admin\/t\/[a-zA-Z0-9-]+$/.test(value) ? value : fallback
  return ['/report', '/me'].includes(value) || /^\/t\/[a-zA-Z0-9-]+$/.test(value) ? value : fallback
}
