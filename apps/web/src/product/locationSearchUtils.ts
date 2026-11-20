export type SearchLocation = { code: string; label: string; region?: string; aliases?: string[]; building_code?: string }
const normalize = (value: string) => value.trim().toLocaleLowerCase().replace(/\s+/g, '')
const numberOf = (item: SearchLocation) => item.building_code || item.label.match(/\d+(?:-\d+)?/)?.[0] || ''
const collator = new Intl.Collator('ko', { numeric: true, sensitivity: 'base' })
export function searchLocations(items: SearchLocation[], query: string) {
  const q = normalize(query)
  return items.filter(item => !q || [numberOf(item), item.label, ...(item.aliases ?? [])].some(value => normalize(value).includes(q)))
    .sort((a, b) => {
      const rank = (item: SearchLocation) => normalize(numberOf(item)) === q && q ? 0 : normalize(numberOf(item)).startsWith(`${q}-`) && q ? 1 : 2
      return rank(a) - rank(b) || collator.compare(numberOf(a), numberOf(b)) || collator.compare(a.label, b.label)
    }).slice(0, 8)
}
