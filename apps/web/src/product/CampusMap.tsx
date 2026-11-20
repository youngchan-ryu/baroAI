import { useEffect, useState } from 'react'
import './campusMap.css'

type Point = { latitude: number; longitude: number; label: string; accuracy?: number; group?: string | null; sequence?: number | null }
type Feature = { properties: { kind: string }; geometry: { type: string; coordinates: unknown } }
type Campus = { name: string; file: string; bounds: [number, number, number, number] }
const campuses: Campus[] = [
  { name: '관악', file: 'gwanak', bounds: [126.94,37.44,126.96,37.47] },
  { name: '연건', file: 'yeongeon', bounds: [126.995,37.574,127.005,37.583] },
  { name: '평창', file: 'pyeongchang', bounds: [128.432,37.540,128.447,37.554] },
  { name: '시흥', file: 'siheung', bounds: [126.712,37.358,126.727,37.371] },
  { name: '수원', file: 'suwon', bounds: [126.981,37.263,126.993,37.273] },
]
const contains = (c: Campus, p: Point) => p.longitude >= c.bounds[0] && p.longitude <= c.bounds[2] && p.latitude >= c.bounds[1] && p.latitude <= c.bounds[3]
function lines(geometry: Feature['geometry']): number[][][] {
  const c = geometry.coordinates as number[][][][]
  if (geometry.type === 'LineString') return [c as unknown as number[][]]
  if (geometry.type === 'MultiLineString' || geometry.type === 'Polygon') return c as unknown as number[][][]
  if (geometry.type === 'MultiPolygon') return c.flat()
  return []
}
export function CampusMap({ points }: { points: Point[] }) {
  const [selected, setSelected] = useState('')
  const available = campuses.filter(c => points.some(p => contains(c, p)))
  const campus = available.find(c => c.file === selected) ?? available[0]
  const [loaded, setLoaded] = useState<{ file: string; features: Feature[]; failed?: boolean } | null>(null)
  const features = loaded && campus && loaded.file === campus.file ? loaded.features : []
  useEffect(() => {
    let active = true
    if (campus) fetch(`/maps/${campus.file}.geojson`).then(r => r.ok ? r.json() : Promise.reject()).then(data => { if (active) setLoaded({ file: campus.file, features: data.features ?? [] }) }).catch(() => { if (active) setLoaded({ file: campus.file, features: [], failed: true }) })
    return () => { active = false }
  }, [campus])
  if (!campus) return <p className="notice">{points.some(p => p.accuracy != null) ? `현재 위치가 캠퍼스 지도 범위 밖이에요. 정확도 약 ${Math.round(points.find(p => p.accuracy != null)!.accuracy!)}m.` : '표시할 수 있는 캠퍼스 지도 범위 밖이거나 좌표가 없어요. 위치 설명을 확인해 주세요.'}</p>
  const [west, south, east, north] = campus.bounds
  const xy = (lon: number, lat: number) => [(lon-west)/(east-west)*600, (north-lat)/(north-south)*500]
  const visible = points.filter(p => contains(campus, p))
  const groups = new Map<string, Point[]>()
  visible.forEach(p => { if (p.group && p.sequence != null) groups.set(p.group, [...(groups.get(p.group) ?? []), p]) })
  return <div className="campus-map">{available.length > 1 && <div aria-label="캠퍼스 선택">{available.map(c => <button type="button" className="button secondary" key={c.file} aria-pressed={campus.file === c.file} onClick={() => setSelected(c.file)}>{c.name}</button>)}</div>}<p>{campus.name} 캠퍼스 · {loaded?.file !== campus.file ? '지도 불러오는 중' : loaded.failed ? '지도 배경을 불러오지 못했어요' : '건물과 길'}</p><svg viewBox="0 0 600 500" role="img" aria-label={`${campus.name} 캠퍼스 지도. ${visible.map(p => `${p.sequence == null ? '' : `${p.sequence + 1}. `}${p.label}`).join(', ')}`}>
    <rect width="600" height="500" fill="#eef3ed" />
    {features.flatMap((f, i) => lines(f.geometry).map((line, j) => <polyline key={`${i}-${j}`} points={line.map(([lon, lat]) => xy(lon,lat).join(',')).join(' ')} fill={f.properties.kind === 'building' ? '#d5ded5' : 'none'} stroke={f.properties.kind === 'building' ? '#b8c7bb' : '#fff'} strokeWidth={f.properties.kind === 'building' ? 1 : 2} />))}
    {[...groups.values()].flatMap((group, i) => { const ordered = group.sort((a,b) => (a.sequence ?? 0)-(b.sequence ?? 0)); return ordered.slice(1).flatMap((p, j) => p.sequence === (ordered[j].sequence ?? -2) + 1 ? [<polyline key={`${i}-${j}`} points={[ordered[j], p].map(item => xy(item.longitude,item.latitude).join(',')).join(' ')} fill="none" stroke="#2b66c9" strokeWidth="3" strokeDasharray="7 5" />] : []) })}
    {visible.map((p,i) => { const [x,y] = xy(p.longitude,p.latitude); const rx = (p.accuracy ?? 0) / (111320 * Math.cos(p.latitude * Math.PI / 180)) / (east-west) * 600; const ry = (p.accuracy ?? 0) / 111320 / (north-south) * 500; return <g key={i}>{rx > 0 && ry > 0 && <ellipse cx={x} cy={y} rx={rx} ry={ry} fill="#2876dc" fillOpacity=".15" stroke="#2876dc" />}<circle cx={x} cy={y} r={p.sequence == null ? 7 : 11} fill="#1759b0" stroke="white" strokeWidth="2" />{p.sequence != null && <text x={x} y={y} fill="white" dominantBaseline="central" textAnchor="middle" fontSize="12" fontWeight="700">{p.sequence + 1}</text>}<text x={x+(p.sequence == null ? 10 : 15)} y={y-8} fontSize="13" paintOrder="stroke" stroke="white" strokeWidth="4" strokeLinejoin="round" fill="#18334d">{p.label}</text></g> })}
  </svg>{visible.some(p => p.accuracy != null) && <p className="field-hint">현재 위치 정확도 약 {Math.round(visible.find(p => p.accuracy != null)!.accuracy!)}m · 범위는 참고용이에요.</p>}<p className="field-hint">{groups.size > 0 && '점선은 승인된 방문 순서이며 실제 이동 경로가 아니에요. '}© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>, ODbL 1.0</p>{points.length > visible.length && <p>다른 캠퍼스 또는 지도 범위 밖 위치는 목록에서 확인해 주세요.</p>}</div>
}
export function Directions({ point }: { point: Point }) {
  if (!campuses.some(c => contains(c, point))) return <p className="field-hint">좌표가 없거나 지도 범위 밖이에요. 서울대 캠퍼스맵에서 동번호를 검색해 주세요. <a href="https://map.snu.ac.kr/web/main.action" target="_blank" rel="noopener noreferrer">서울대 캠퍼스맵</a></p>
  return <a className="button secondary" href={`https://map.kakao.com/link/to/${encodeURIComponent("방문 건물")},${point.latitude},${point.longitude}`} target="_blank" rel="noopener noreferrer">카카오맵 길찾기</a>
}
