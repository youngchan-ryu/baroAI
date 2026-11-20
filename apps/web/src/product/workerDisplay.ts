const legacyCampusNames = new Map<string, string>([
  ['가상 전기 작업자', '전기 담당 1'],
  ['가상 설비 작업자', '설비 담당 1'],
  ['가상 건축 작업자', '건축 담당 1'],
  ['가상 토목 작업자', '토목 담당 1'],
])

export function workerDisplayName(name: string, mode: string): string {
  return mode === 'demo' ? legacyCampusNames.get(name) ?? name : name
}

export function workerDisplayLabel(name: string, code: string, names: string[], mode: string): string {
  const display = workerDisplayName(name, mode)
  return names.filter(value => workerDisplayName(value, mode) === display).length > 1 ? `${display} · ${code}` : display
}

/** Labels only duplicate visible names; identity and selection remain tied to IDs. */
export function workerDistinctLabel(id: string, name: string, peers: { id: string; name: string }[], mode: string): string {
  const display = workerDisplayName(name, mode)
  const ids = [...new Set(peers.filter(peer => workerDisplayName(peer.name, mode) === display).map(peer => peer.id))].sort()
  return ids.length > 1 && ids.includes(id) ? `${display} · ${ids.indexOf(id) + 1}` : display
}
