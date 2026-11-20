import { useOptionalRuntime } from './runtime'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, ApiError, request, uploadPath, type Auth } from './api'
import type { Photo } from './types'
import { MemoryDraft, useMemoryDraft } from './memoryDraft'
import { PhotoProtection, type PhotoProtectionDraft } from './PhotoProtection'
import { ErrorNotice } from './shared'

const acceptedImageTypes = new Set([
  'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif',
  'image/heic-sequence', 'image/heif-sequence',
])

function acceptedImage(file: File) {
  const type = file.type.toLowerCase()
  if (acceptedImageTypes.has(type)) return true
  return (type === '' || type === 'application/octet-stream') && /\.hei[cf]$/i.test(file.name)
}

// eslint-disable-next-line react-refresh/only-export-components
export function useUploads(memory?: MemoryDraft, photoLocation = false) {
  const [local] = useState(() => { const value = new MemoryDraft(); value.activate('local-upload'); return value })
  const store = memory ?? local
  const data = useMemoryDraft(store)
  const token = store.token()
  const guardKey = `${token.owner}:${token.generation}`
  const isCurrent = useCallback(() => store.current(token), [store, token.owner, token.generation]) // eslint-disable-line react-hooks/exhaustive-deps
  const setFiles = (files: File[]) => {
    if (data.pending || data.sending || data.sealed) return
    store.update(token, { files, photos: new Map([...data.photos].filter(([file]) => files.includes(file))), uploadKeys: new Map([...data.uploadKeys].filter(([file]) => files.includes(file))), abandonedUploads: new Set([...data.abandonedUploads].filter(file => files.includes(file))), protection: new Map([...data.protection].filter(([file]) => files.includes(file))) })
  }
  async function upload(auth: Auth) {
    if (!isCurrent() || store.snapshot().uploadCount) throw new Error('upload_not_current')
    store.update(token, value => ({ uploadCount: value.uploadCount + 1 }))
    const files = [...store.snapshot().files]
    try {
      for (const file of files) {
        if (!isCurrent()) throw new Error('draft_not_current')
        if (!store.snapshot().files.includes(file)) continue
        if (!store.snapshot().photos.has(file)) {
          if (store.snapshot().abandonedUploads.has(file)) throw new ApiError('operation_abandoned', 409)
          let key = store.snapshot().uploadKeys.get(file)
          if (!key) { key = crypto.randomUUID(); store.update(token, value => ({ uploadKeys: new Map(value.uploadKeys).set(file, key!) })) }
          let photo: Photo
          try { photo = await api.upload(file, auth, key, photoLocation, photoLocation && store.snapshot().photoCoordinatesAllowed) } catch (error) {
            if (!isCurrent()) throw new Error('draft_not_current', { cause: error })
            if (!store.snapshot().files.includes(file)) continue
            if (error instanceof ApiError && error.code === 'operation_abandoned') store.update(token, value => ({ abandonedUploads: new Set(value.abandonedUploads).add(file) }))
            throw error
          }
          if (!isCurrent()) throw new Error('draft_not_current')
          if (!store.snapshot().files.includes(file)) continue
          store.update(token, value => ({ photos: new Map(value.photos).set(file, photo) }))
        }
      }
      if (!isCurrent()) throw new Error('draft_not_current')
      const selected = store.snapshot().files
      if (selected.some(file => !store.snapshot().photos.has(file))) throw new Error('draft_not_current')
      return selected.map(file => store.snapshot().photos.get(file)!.id)
    } finally { store.update(token, value => ({ uploadCount: Math.max(0, value.uploadCount - 1) })) }
  }
  const update = (file: File, photo: Photo) => { if (!store.snapshot().files.includes(file)) return; store.update(token, value => ({ photos: new Map(value.photos).set(file, photo) })) }
  const setProtecting = (busy: boolean) => { store.update(token, value => ({ protectionCount: Math.max(0, value.protectionCount + (busy ? 1 : -1)) })) }
  const setProtectionDraft = (file: File, value: PhotoProtectionDraft) => { if (!store.snapshot().files.includes(file)) return; if (JSON.stringify(store.snapshot().protection.get(file)) === JSON.stringify(value)) return; store.update(token, previous => ({ protection: new Map(previous.protection).set(file, value) })) }
  async function revalidate(auth: Auth) {
    if (!isCurrent()) return
    store.update(token, value => ({ uploadCount: value.uploadCount + 1 }))
    try {
      const refreshed = new Map<File, Photo>()
      for (const [file, photo] of store.snapshot().photos) {
        const latest = await request<Photo>(uploadPath(auth, photo.id, 'metadata'), {}, auth)
        if (!isCurrent()) return
        refreshed.set(file, latest)
      }
      store.update(token, value => ({ photos: new Map([...refreshed].filter(([file]) => value.files.includes(file))), photosValidated: true }))
    } finally { store.update(token, value => ({ uploadCount: Math.max(0, value.uploadCount - 1) })) }
  }
  return { files: data.files, hasUnpreparedFiles: () => store.snapshot().files.some(file => !store.snapshot().photos.has(file)), setFiles, upload, abandoned: data.abandonedUploads.size > 0, photos: data.photos, update, preparing: data.uploadCount > 0, protecting: data.protectionCount > 0, setProtecting, ready: data.photosValidated && data.files.every(file => data.photos.has(file)), protection: data.protection, setProtectionDraft, guardKey, isCurrent, revalidate, needsRevalidation: !data.photosValidated, pending: Boolean(data.pending) || data.sealed }
}
export function UploadField({ uploads,auth,disabled,autoPrepare=false }: { uploads:ReturnType<typeof useUploads>;auth:Auth;disabled:boolean;autoPrepare?:boolean }) {
  const runtime=useOptionalRuntime()
  const {t}=useTranslation('product')
  const [invalid,setInvalid]=useState(false)
  const [error,setError]=useState<unknown>(null)
  const attempted = useRef<File[]>([])
  const {files,setFiles}=uploads
  useEffect(() => {
    if (!uploads.pending && uploads.needsRevalidation && !uploads.preparing) void uploads.revalidate(auth).catch(value => { if (uploads.isCurrent()) setError(value) })
  // Revalidate once per authenticated generation; a failure requires explicit retry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploads.guardKey])
  useEffect(() => {
    if (autoPrepare && !uploads.pending && !uploads.needsRevalidation && !uploads.preparing && !uploads.ready && !error && !uploads.abandoned && files.some(file => !attempted.current.includes(file))) {
      attempted.current = [...files]
      void uploads.upload(auth).catch(value => { if (uploads.isCurrent() && uploads.hasUnpreparedFiles()) setError(value) })
    }
  // Selection and upload state drive preparation; failures require an explicit retry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files, uploads.guardKey, uploads.pending, uploads.needsRevalidation, uploads.preparing, uploads.ready, uploads.abandoned, error])
  return <fieldset className="upload-field" disabled={disabled||uploads.protecting||(!autoPrepare && uploads.preparing)}><legend>{t('photosLabel')} <span className="optional-label">({t('optional')})</span></legend><p className="field-hint">{autoPrepare ? '얼굴·명찰·문서는 가리고 찍어 주세요.' : t('photosHint')}</p><div className="upload-zone"><label className="button secondary upload-button">📷 사진 찍기 · 앨범에서 고르기<input disabled={uploads.preparing} type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif,image/heic-sequence,image/heif-sequence,.heic,.heif" multiple onChange={event=>{const next=[...files,...Array.from(event.currentTarget.files??[])];if(next.length>3||next.some(file=>file.size>10*1024*1024||!acceptedImage(file)))setInvalid(true);else{setInvalid(false);setFiles(next)}event.currentTarget.value=''}}/></label><ul className="file-list">{files.map((file,index)=><li key={`${file.name}-${index}`}><span>{file.name}</span><button type="button" className="remove-file" aria-label={`${t('removePhoto')} ${file.name}`} onClick={()=>{setError(null);attempted.current=attempted.current.filter(item=>item!==file);setFiles(files.filter((_,i)=>i!==index))}}>×</button></li>)}</ul></div>{invalid&&<p className="field-error" role="alert">{t('fileInvalid')}</p>}{uploads.needsRevalidation && <button type="button" className="button secondary" onClick={() => { setError(null); void uploads.revalidate(auth).catch(value => { if (uploads.isCurrent()) setError(value) }) }}>{t('draftPhotosRefresh')}</button>}{!autoPrepare && !uploads.ready && !uploads.needsRevalidation && <button type="button" className="button secondary" disabled={uploads.abandoned} onClick={()=>{setError(null);void uploads.upload(auth).catch(value => { if (uploads.isCurrent()) setError(value) })}}>{t(uploads.preparing?'preparing':'preparePhotos')}</button>}{uploads.preparing && autoPrepare && <p role="status" className="field-hint">{t('preparing')}</p>}{autoPrepare && Boolean(error) && !uploads.abandoned && <button type="button" className="text-button" onClick={()=>{setError(null);attempted.current=[]}}>다시 준비</button>}{Boolean(error)&&<ErrorNotice error={error}/>}{!autoPrepare && <p className="field-hint privacy-hint">{t(runtime?.mode === 'service' ? 'servicePhotoPrivacy' : 'privacy')}</p>}{files.map((file,index)=>{const photo=uploads.photos.get(file);return photo&&!uploads.needsRevalidation?<PhotoProtection key={`${photo.id}:${uploads.guardKey}`} photo={photo} draft={uploads.protection.get(file)} onDraftChange={value=>uploads.setProtectionDraft(file,value)} guardKey={uploads.guardKey} isCurrent={uploads.isCurrent} auth={auth} onUpdate={updated=>uploads.update(file,updated)} remove={()=>setFiles(files.filter((_,i)=>i!==index))} disabled={disabled || uploads.needsRevalidation} onBusy={uploads.setProtecting}/>:null})}</fieldset>
}
