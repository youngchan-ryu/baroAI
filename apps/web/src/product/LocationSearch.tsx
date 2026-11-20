import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { searchLocations, type SearchLocation } from './locationSearchUtils'
import './locationSearch.css'

export function LocationSearch({ label, items, value, onChange, disabled = false, textValue, onTextChange, onQuery, remoteOnly = false, searchStatus = 'idle', placeholder }: { label: ReactNode; items: SearchLocation[]; value: string; onChange: (code: string) => void; disabled?: boolean; textValue?: string; onTextChange?: (text: string) => void; onQuery?: (text: string) => void; remoteOnly?: boolean; searchStatus?: 'idle' | 'loading' | 'error' | 'unavailable' | 'ready'; placeholder?: string }) {
  const id = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const isDisabled = () => disabled || Boolean(inputRef.current?.matches(':disabled'))
  const selected = items.find(item => item.code === value)
  const [query, setQuery] = useState(selected?.label ?? '')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState<string | null>(null)
  const options = remoteOnly ? items.slice(0, 8) : searchLocations(items, query)
  const grouped = new Set(options.map(item => item.region).filter(Boolean)).size > 1
  const groups = grouped ? [...new Set(options.map(item => item.region ?? ''))] : ['']
  const ordered = grouped ? groups.flatMap(region => options.filter(item => (item.region ?? '') === region)) : options
  const activeIndex = ordered.findIndex(item => item.code === active)
  const regionLabel = (region: string) => ({ GWANAK: '관악', YEONGEON: '연건', PYEONGCHANG: '평창', SUWON: '수원' } as Record<string, string>)[region] ?? '기타 권역'
  useEffect(() => {
    if (open && !disabled && activeIndex >= 0) document.getElementById(`${id}-${activeIndex}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [open, disabled, activeIndex, id])
  const choose = (item: SearchLocation) => { if (isDisabled()) return; onChange(item.code); setQuery(item.label); setOpen(false); setActive(null) }
  return <div className="location-search"><label htmlFor={id}>{label}</label><input id={id} ref={inputRef} role="combobox" aria-autocomplete="list" aria-expanded={open && !disabled} aria-controls={`${id}-list`} aria-activedescendant={open && !disabled && activeIndex >= 0 ? `${id}-${activeIndex}` : undefined} autoComplete="off" placeholder={placeholder} disabled={disabled} value={textValue ?? (open ? query : selected?.label ?? '')} onFocus={() => { if (isDisabled()) return; setQuery(textValue ?? selected?.label ?? ''); setOpen(true) }} onBlur={() => { setOpen(false); setQuery(selected?.label ?? '') }} onChange={event => { if (isDisabled()) return; setQuery(event.target.value); onTextChange?.(event.target.value); onQuery?.(event.target.value); setOpen(true); setActive(null) }} onKeyDown={event => {
    if (isDisabled()) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setOpen(true); setActive(current => { const index = ordered.findIndex(item => item.code === current); const next = event.key === 'ArrowDown' ? Math.min(index + 1, ordered.length - 1) : Math.max(index - 1, 0); return ordered[next]?.code ?? null }) }
    else if (event.key === 'Enter' && open && activeIndex >= 0 && ordered[activeIndex]) { event.preventDefault(); choose(ordered[activeIndex]) }
    else if (event.key === 'Escape') { setOpen(false); setQuery(selected?.label ?? '') }
  }} />{open && !disabled && <ul className="location-search-options" id={`${id}-list`} role="listbox">{groups.map(region => <li key={region || 'all'} role="presentation">{grouped && <strong className="location-search-group">{regionLabel(region)}</strong>}<ul role="group" aria-label={grouped ? regionLabel(region) : undefined}>{ordered.filter(item => !grouped || (item.region ?? '') === region).map(item => { const index = ordered.indexOf(item); return <li id={`${id}-${index}`} role="option" aria-selected={item.code === value} key={item.code} className={index === activeIndex ? 'active' : undefined} onMouseDown={event => event.preventDefault()} onClick={() => choose(item)}>{item.label}</li> })}</ul></li>)}{searchStatus === 'loading' && <li className="location-search-empty" role="status">위치를 찾고 있어요.</li>}{searchStatus === 'error' && <li className="location-search-empty" role="status">위치 검색을 불러오지 못했어요.</li>}{searchStatus === 'unavailable' && <li className="location-search-empty" role="status">현재 검색 자료를 확인할 수 없어요.</li>}{options.length === 0 && searchStatus === 'ready' && <li className="location-search-empty">일치하는 위치가 없어요.</li>}{options.length === 0 && searchStatus === 'idle' && !remoteOnly && <li className="location-search-empty">일치하는 위치가 없어요.</li>}</ul>}</div>
}
