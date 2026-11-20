import { useEffect, useLayoutEffect, useState, useRef, type PointerEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { request, imageRequest, uploadPath, type Auth } from './api'
import { ErrorNotice } from './shared'
import { dateTime } from './dateTime'
import type { Photo } from './types'
import type { Mask } from './privacyTypes'

export type PhotoProtectionDraft = {source:boolean;masks:Mask[];coords:{x:number;y:number;width:number;height:number};appliedMasks?:Mask[];appliedVariant?:string|null}

export function PhotoProtection({ photo, auth, onUpdate, remove, disabled, onBusy, draft, onDraftChange, guardKey, isCurrent }: {photo:Photo;auth:Auth;onUpdate:(photo:Photo)=>void;remove?:()=>void;disabled:boolean;onBusy:(busy:boolean)=>void;draft?:PhotoProtectionDraft;onDraftChange?:(draft:PhotoProtectionDraft)=>void;guardKey?:string;isCurrent?:()=>boolean}) {
  const {t}=useTranslation('product')
  const [source,setSource]=useState(photo.privacy?.status === 'approved' ? false : draft?.source??false)
  const [previewReady,setPreviewReady]=useState('')
  const [masks,setMasks]=useState<Mask[]>(draft?.masks??[])
  const [coords,setCoords]=useState(draft?.coords??{x:0,y:0,width:20,height:20})
  const [applied, setApplied] = useState(() => ({
    masks: draft?.appliedMasks ?? [],
    variant: draft?.appliedVariant ?? photo.privacy?.variant_id ?? null,
  }))
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState<unknown>(null)
  const [invalid,setInvalid]=useState(false)
  const mutationInFlight=useRef(false)
  const generation=useRef(0)
  const alive=useRef(true)
  const current=useRef({guardKey,isCurrent,authKey:JSON.stringify([auth.bearer,auth.csrf,auth.assignmentId])})
  useLayoutEffect(()=>{current.current={guardKey,isCurrent,authKey:JSON.stringify([auth.bearer,auth.csrf,auth.assignmentId])}},[guardKey,isCurrent,auth.bearer,auth.csrf,auth.assignmentId])
  useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
  const draftCallback=useRef(onDraftChange)
  useLayoutEffect(()=>{draftCallback.current=onDraftChange},[onDraftChange])
  useEffect(()=>{if(current.current.isCurrent?.()??true)draftCallback.current?.({source,masks,coords,appliedMasks:applied.masks,appliedVariant:applied.variant})},[source,masks,coords,applied])
  const privacy=photo.privacy
  async function mutate(action:'process'|'approval'|'revoke', openSource = false, detectFaces = false) {
    if(!privacy || disabled || mutationInFlight.current || !(isCurrent?.()??true)) return
    mutationInFlight.current=true
    const owner=current.current
    const attempt=++generation.current
    const ownerValid=()=>(alive.current||Boolean(owner.isCurrent))&&generation.current===attempt&&current.current.guardKey===owner.guardKey&&current.current.authKey===owner.authKey&&(owner.isCurrent?.()??true)
    const valid=()=>alive.current&&ownerValid()
    setBusy(true);onBusy(true);if(action==='process')setPreviewReady('');setError(null)
    if(action==='process') onUpdate({...photo,privacy:{...privacy,status:'pending',approved_at:null,variant_id:null,sha256:null}})
    try {
      const updated=await request<Photo>(uploadPath(auth, photo.id, action==='revoke'?'approval':action), {method:action==='revoke'?'DELETE':'POST',body:action==='process'?{masks, ...(detectFaces ? { detect_faces: true } : {})}:action==='approval'?{variant_id:privacy.variant_id,sha256:privacy.sha256,pipeline_version:privacy.pipeline_version,provider:privacy.provider,purpose:privacy.purpose}:undefined},auth)
      if(ownerValid())onUpdate(updated)
      if(valid()) {
        if (action === 'process') setApplied({masks: [...masks], variant: updated.privacy?.variant_id ?? null})
        setSource(openSource && updated.privacy?.status === 'revoked')
      }
    }catch(value){if(valid())setError(value)}finally{mutationInFlight.current=false;if(valid())setBusy(false);if(ownerValid())onBusy(false)}
  }
  const showSource = source && privacy?.status !== 'approved'
  const masksDirty = applied.variant !== (privacy?.variant_id ?? null) || JSON.stringify(masks) !== JSON.stringify(applied.masks)
  const processed=privacy && ['processed','approved','revoked'].includes(privacy.status) && privacy.variant_id
  return <article className="photo-protection"><div className="section-title"><strong>{t('processedPreview')}</strong><span className="muted-label">{privacy?t(`photoState.${privacy.status}`):t('preparing')}</span></div>{processed && <PrivacyPreview key={`${privacy.variant_id}-${showSource}`} id={photo.id} variant={privacy.variant_id!} source={showSource} auth={auth} masks={showSource?masks:[]} onMask={showSource && !busy && !disabled ? mask => setMasks(previous => [...previous, mask]) : undefined} guardKey={guardKey} isCurrent={isCurrent} onReady={ready=>setPreviewReady(ready&&!showSource?privacy.variant_id!: '')} />}{!processed && <p className="notice">{t('previewUnavailable')}</p>}<p className="field-hint">{t('protectHint')}</p><fieldset disabled={busy||disabled}><button type="button" className="text-button" onClick={()=>{if(showSource){setSource(false)}else if(privacy?.status==='approved'){void mutate('revoke', true)}else{setSource(true)}}}>{showSource ? t('processedPreview') : privacy?.status==='approved' ? '가림 수정 · 기존 AI 처리 승인 철회' : t('sourcePreview')}</button>{showSource && <><p className="field-hint">{t('sourceNote')}</p>{!processed && <PrivacyPreview id={photo.id} source auth={auth} masks={masks} onMask={!busy && !disabled ? mask => setMasks(previous => [...previous, mask]) : undefined} guardKey={guardKey} isCurrent={isCurrent} />}<p className="field-hint">{t('maskHint')}</p><details><summary>키보드로 가림 영역 입력</summary><div className="mask-coordinates">{(['x','y','width','height'] as const).map(key=><label key={key}>{t({x:'maskX',y:'maskY',width:'maskWidth',height:'maskHeight'}[key])}<input type="number" min={0} max={100} step={1} value={coords[key]} onChange={event=>setCoords({...coords,[key]:Number(event.target.value)})} /></label>)}</div><button className="button secondary" type="button" onClick={()=>{if(coords.x<0||coords.y<0||coords.width<=0||coords.height<=0||coords.x+coords.width>100||coords.y+coords.height>100){setInvalid(true);return}setInvalid(false);setMasks([...masks,{x:coords.x/100,y:coords.y/100,width:coords.width/100,height:coords.height/100}])}}>{t('addMask')}</button></details>{invalid&&<p role="alert" className="field-error">{t('maskInvalid')}</p>}<ol className="mask-list">{masks.map((mask,index)=><li key={index}>{Math.round(mask.x*100)}%, {Math.round(mask.y*100)}% · {Math.round(mask.width*100)}% × {Math.round(mask.height*100)}%<button type="button" className="remove-file" aria-label={`${t('removePhoto')} ${index+1}`} onClick={()=>setMasks(masks.filter((_,i)=>i!==index))}>×</button></li>)}</ol></>}{(showSource||!processed)&&<button type="button" className="button secondary full-width" onClick={()=>void mutate('process')}>{t(busy?'preparing':'applyMasks')}</button>}{privacy && privacy.status !== 'approved' && <button type="button" className="button secondary" onClick={()=>void mutate('process', false, true)}>얼굴 자동 가림</button>}{privacy && <><dl className="privacy-meta"><div><dt>{t('provider')}</dt><dd>{privacy.provider === 'openai' ? 'OpenAI' : privacy.provider}</dd></div><div><dt>{t('purpose')}</dt><dd>{privacy.purpose?t(privacy.purpose,{defaultValue:privacy.purpose}):'—'}</dd></div>{privacy.delete_after&&<div><dt>{t('deletion')}</dt><dd>{dateTime(privacy.delete_after, 'full')}</dd></div>}</dl><details className="privacy-record"><summary>{t('processingDetails')}</summary><dl className="privacy-meta">{privacy.variant_id&&<div><dt>{t('variant')}</dt><dd>{privacy.variant_id}</dd></div>}{privacy.pipeline_version&&<div><dt>{t('pipeline')}</dt><dd>{privacy.pipeline_version}</dd></div>}{privacy.sha256&&<div><dt>{t('hash')}</dt><dd>{privacy.sha256}</dd></div>}</dl></details>{privacy.detector_version !== 'manual-only' && privacy.face_count != null &&<p className="field-hint">{t('faces',{count:privacy.face_count})}</p>}<p className="field-hint">{t('consentHint')}</p>{processed && privacy.status!=='approved' && <button type="button" className="button secondary full-width" disabled={showSource||masksDirty||previewReady!==privacy.variant_id||!privacy.provider||!privacy.purpose} onClick={()=>void mutate('approval')}>{t('approvePhoto')}</button>}{privacy.status==='approved'&&<><button type="button" className="button secondary full-width" onClick={()=>void mutate('revoke')}>{t('revokePhoto')}</button><p className="field-hint">{t('revokedHint')}</p></>}</>}{remove&&<button type="button" className="text-button" onClick={remove}>{t('skipPhoto')}</button>}</fieldset>{Boolean(error)&&<ErrorNotice error={error}/>}</article>
}
function PrivacyPreview({id,variant,source,auth,masks,onMask,onReady,guardKey,isCurrent}:{id:string;variant?:string;source?:boolean;auth:Auth;masks:Mask[];onMask?:(mask:Mask)=>void;onReady?:(ready:boolean)=>void;guardKey?:string;isCurrent?:()=>boolean}) {
  const {t}=useTranslation('product')
  const [url,setUrl]=useState('')
  const [error,setError]=useState(false)
  const [attempt,setAttempt]=useState(0)
  const [drawing,setDrawing]=useState<Mask | null>(null)
  const start=useRef<{x:number;y:number;pointer:number} | null>(null)
  function point(event:PointerEvent<HTMLDivElement>) {
    const rect=event.currentTarget.getBoundingClientRect()
    return {x:Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),y:Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))}
  }
  function move(event:PointerEvent<HTMLDivElement>) {
    if (!onMask || !start.current || start.current.pointer!==event.pointerId) return
    const p=point(event), a=start.current
    setDrawing({x:Math.min(a.x,p.x),y:Math.min(a.y,p.y),width:Math.abs(a.x-p.x),height:Math.abs(a.y-p.y)})
  }
  const displayed=useRef(url)
  useLayoutEffect(()=>{displayed.current=url},[url])
  const readyCallback=useRef(onReady)
  const current=useRef({guardKey,isCurrent})
  useLayoutEffect(()=>{current.current={guardKey,isCurrent};readyCallback.current=onReady},[guardKey,isCurrent,onReady])
  const bearer=auth.bearer
  const csrf=auth.csrf
  const assignmentId=auth.assignmentId
  useEffect(()=>{
    let active=true;let object='';const abort=new AbortController()
    const owner=guardKey
    const valid=()=>active&&current.current.guardKey===owner&&(current.current.isCurrent?.()??true)
    if(!valid())return()=>{active=false;abort.abort()}
    readyCallback.current?.(false)
    queueMicrotask(()=>{if(valid()){setUrl('');setError(false)}})
    const path=source?'source':`variants/${variant}`
    imageRequest(uploadPath({bearer,csrf,assignmentId}, id, path), {bearer,csrf,assignmentId}, abort.signal, valid).then(blob=>{if(valid()){object=URL.createObjectURL(blob);setUrl(object)}}).catch(()=>{if(valid()){readyCallback.current?.(false);setError(true)}})
    return()=>{active=false;abort.abort();if(object)URL.revokeObjectURL(object)}
  },[id,variant,source,bearer,csrf,assignmentId,guardKey,attempt])
  function failed(){readyCallback.current?.(false);setError(true);setUrl('')}
  if(error)return <div><p className="field-error">{t('photoFailed')}</p><button type="button" className="button secondary" onClick={()=>{readyCallback.current?.(false);setAttempt(value=>value+1)}}>{t('photoPreviewRetry')}</button></div>
  return <div className="protection-preview">{url?<div className={`preview-frame${onMask ? ' drawable' : ''}`} onPointerDown={event=>{if(!onMask || event.button!==0)return; const p=point(event);start.current={...p,pointer:event.pointerId};event.currentTarget.setPointerCapture(event.pointerId)}} onPointerMove={move} onPointerUp={event=>{if(!onMask || !start.current || start.current.pointer!==event.pointerId){start.current=null;setDrawing(null);return;}const p=point(event),a=start.current;const mask={x:Math.min(a.x,p.x),y:Math.min(a.y,p.y),width:Math.abs(a.x-p.x),height:Math.abs(a.y-p.y)};start.current=null;setDrawing(null);if(mask.width>0.005 && mask.height>0.005)onMask?.(mask)}} onPointerCancel={()=>{start.current=null;setDrawing(null)}}><img draggable={false} src={url} alt={t(source?'source':'processedPreview')} onError={()=>{if(displayed.current===url&&current.current.guardKey===guardKey&&(current.current.isCurrent?.()??true))failed()}} onLoad={()=>{if(displayed.current===url&&current.current.guardKey===guardKey&&(current.current.isCurrent?.()??true))readyCallback.current?.(true)}}/>{[...masks,...(drawing?[drawing]:[])].map((mask,index)=><span key={index} className="opaque-mask" style={{left:`${mask.x*100}%`,top:`${mask.y*100}%`,width:`${mask.width*100}%`,height:`${mask.height*100}%`}}/>)}</div>:<p>{t('loading')}</p>}</div>
}
