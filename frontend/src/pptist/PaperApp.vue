<template>
  <div class="paper-pptist-app" ref="appRootRef">
    <div
      class="paper-pptist-status"
      v-if="statusText || errorText"
      :class="{ error: !!errorText, busy: statusBusy, success: statusSaved }"
    >
      <span v-if="statusBusy" class="paper-pptist-status-spinner" aria-hidden="true"></span>
      <span v-else-if="statusSaved" class="paper-pptist-status-check" aria-hidden="true">✓</span>
      <span>{{ errorText || statusText }}</span>
    </div>
    <template v-if="slides.length">
      <Teleport v-if="screening" to="body">
        <div class="paper-pptist-screen-portal">
          <Screen />
        </div>
      </Teleport>
      <Editor v-else />
    </template>
    <FullscreenSpin :tip="pptistT('pptist.opening')" v-else loading :mask="false" />
  </div>
</template>

<script lang="ts" setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { nanoid } from 'nanoid'
import { useMainStore, useScreenStore, useSlidesStore, useSnapshotStore } from '@/store'
import { deleteDiscardedDB } from '@/utils/database'
import useExport from '@/hooks/useExport'
import useImport from '@/hooks/useImport'
import type { Slide, SlideBackground } from '@/types/slides'
import { installPptistDomI18n, pptistT, type PptistLocale } from './i18n'

import Editor from './views/Editor/index.vue'
import Screen from './views/Screen/index.vue'
import FullscreenSpin from '@/components/FullscreenSpin.vue'

type StudioKind = 'preview' | 'templateImport'

interface PptistStudioOptions {
  kind: StudioKind
  id: string
  locale?: PptistLocale
  controller?: {
    save?: () => Promise<void>
    exportPptx?: () => Promise<void>
  }
  downloadUrl?: string
  onConfirmImport?: () => Promise<void> | void
  saveBeforeConfirmImport?: boolean
  confirmImportDisabled?: boolean
  confirmImportHint?: string
  onCancelImport?: () => void
  onReexport?: () => void
  onDeleteRun?: () => Promise<void> | void
  onStatus?: (message: string) => void
  onSaved?: (result: unknown) => void
  onError?: (message: string) => void
}

interface PptistDeckPayload {
  title?: string
  width?: number
  height?: number
  theme?: Record<string, unknown> | null
  slides?: Slide[]
  source?: {
    source_pptx_url?: string | null
    fallback_slides?: Array<Record<string, unknown>>
    saved_deck?: boolean
  }
}

const props = defineProps<{
  options: PptistStudioOptions
}>()

;(window as any).__PAPER_PPTIST_LOCALE__ = props.options.locale || 'zh'

const mainStore = useMainStore()
const slidesStore = useSlidesStore()
const snapshotStore = useSnapshotStore()
const screenStore = useScreenStore()
const { slides, title, theme, viewportRatio, viewportSize } = storeToRefs(slidesStore)
const { screening } = storeToRefs(screenStore)
const { exportPPTX } = useExport()
const { importPPTXFile, exporting: importing } = useImport()

const statusText = ref('')
const errorText = ref('')
const statusKind = ref<'idle' | 'busy' | 'success' | 'info' | 'error'>('idle')
const loadedFallbackSlides = ref<Array<Record<string, unknown>>>([])
const appRootRef = ref<HTMLElement | null>(null)
let stopDomI18n: (() => void) | undefined
const statusBusy = computed(() => statusKind.value === 'busy' && !!statusText.value && !errorText.value)
const statusSaved = computed(() => statusKind.value === 'success' && !!statusText.value && !errorText.value)
const apiBase = String(import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

const apiUrl = (path: string) => {
  if (!apiBase || /^(https?:|data:|blob:)/i.test(path)) return path
  return path.startsWith('/') ? `${apiBase}${path}` : `${apiBase}/${path}`
}

const deckEndpoint = computed(() => {
  if (props.options.kind === 'templateImport') return apiUrl(`/api/templates/import/${props.options.id}/pptist/deck`)
  return apiUrl(`/api/pptist/preview/${props.options.id}/deck`)
})

const exportEndpoint = computed(() => {
  if (props.options.kind === 'templateImport') return apiUrl(`/api/templates/import/${props.options.id}/pptist/export`)
  return apiUrl(`/api/pptist/preview/${props.options.id}/export`)
})

const paperDownloadUrl = computed(() => {
  if (props.options.downloadUrl) return props.options.downloadUrl
  if (props.options.kind !== 'preview') return ''
  return apiUrl(`/api/download/${props.options.id}`)
})

const setStatus = (message: string, kind: 'busy' | 'success' | 'info' = 'busy') => {
  statusText.value = message
  statusKind.value = message ? kind : 'idle'
  props.options.onStatus?.(message)
}

const setError = (message: string) => {
  errorText.value = message
  statusKind.value = 'error'
  props.options.onError?.(message)
}

const loadDeck = async () => {
  setStatus(pptistT('pptist.loadingData'))
  const response = await fetch(deckEndpoint.value)
  if (!response.ok) throw new Error(await response.text())
  const payload = await response.json() as PptistDeckPayload
  loadedFallbackSlides.value = payload.source?.fallback_slides || []

  if (payload.width && payload.height) {
    slidesStore.setViewportSize(payload.width)
    slidesStore.setViewportRatio(payload.height / payload.width)
  }
  if (payload.title) slidesStore.setTitle(payload.title)
  if (payload.theme) slidesStore.setTheme(payload.theme as any)

  if (payload.slides?.length) {
    slidesStore.setSlides(payload.slides)
    await initSnapshots()
    await maybeAutoSaveInitialDeck(payload)
    setStatus('')
    return
  }

  const sourceUrl = payload.source?.source_pptx_url
  if (sourceUrl) {
    try {
      await importSourcePptx(sourceUrl)
      if (slides.value.length) {
        applyInvalidSlidePlaceholders(payload.width || 1280, payload.height || 720)
        await initSnapshots()
        await maybeAutoSaveInitialDeck(payload)
        setStatus('')
        return
      }
    }
    catch (err) {
      console.warn('[PPTist] PPTX bootstrap failed', err)
    }
  }

  const fallbackSlides = slidesFromFallback(loadedFallbackSlides.value, payload.width || 1280, payload.height || 720)
  slidesStore.setSlides(fallbackSlides.length ? fallbackSlides : [blankSlide()])
  await initSnapshots()
  await maybeAutoSaveInitialDeck(payload)
  setStatus(sourceUrl ? pptistT('pptist.pptxFallback') : '', 'info')
}

const importSourcePptx = async (sourceUrl: string) => {
  setStatus(pptistT('pptist.parsingPptx'))
  const response = await fetch(apiUrl(sourceUrl))
  if (!response.ok) throw new Error(await response.text())
  const blob = await response.blob()
  const file = new File([blob], 'source.pptx', {
    type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  })
  importPPTXFile([file], { cover: true, fixedViewport: false, notifyOnError: false })
  await waitForImport()
}

const waitForImport = async () => {
  const startedAt = Date.now()
  while (importing.value || !slides.value.length) {
    if (Date.now() - startedAt > 45000) throw new Error(pptistT('pptist.importTimedOut'))
    await new Promise(resolve => window.setTimeout(resolve, 150))
  }
}

const initSnapshots = async () => {
  try {
    await deleteDiscardedDB()
    await snapshotStore.initSnapshotDatabase()
  }
  catch (err) {
    console.warn('[PPTist] snapshot init failed', err)
  }
}

const blankSlide = (): Slide => ({
  id: nanoid(10),
  elements: [],
})

const slidesFromFallback = (items: Array<Record<string, unknown>>, width: number, height: number): Slide[] => {
  const ratio = height && width ? height / width : viewportRatio.value
  slidesStore.setViewportSize(width || viewportSize.value)
  slidesStore.setViewportRatio(ratio || 0.5625)

  return items.map((item, itemIndex) => {
    if (item.svg_valid === false) return invalidSvgSlide(item, itemIndex + 1, width || 1280, height || 720)
    const svg = typeof item.content === 'string' ? item.content : ''
    const src = svg ? svgToDataUrl(svg) : String(item.render_url || item.preview_image_url || item.preview_svg_url || '')
    const background: SlideBackground | undefined = src
      ? {
          type: 'image',
          image: {
            src,
            size: 'contain',
          },
        }
      : undefined

    return {
      id: nanoid(10),
      elements: [],
      background,
      remark: typeof item.notes === 'string' ? item.notes : '',
    }
  })
}

const applyInvalidSlidePlaceholders = (width: number, height: number) => {
  const invalidSlides = loadedFallbackSlides.value.filter(item => item.svg_valid === false)
  if (!invalidSlides.length) return

  const nextSlides = [...slides.value]
  for (const item of invalidSlides) {
    const index = fallbackSlideIndex(item, nextSlides.length + 1)
    while (nextSlides.length < index) nextSlides.push(blankSlide())
    nextSlides[index - 1] = invalidSvgSlide(item, index, width || 1280, height || 720)
  }
  slidesStore.setSlides(nextSlides)
}

const fallbackSlideIndex = (item: Record<string, unknown>, fallback: number) => {
  const raw = Number(item.index)
  return Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : fallback
}

const invalidSvgSlide = (item: Record<string, unknown>, index: number, width: number, height: number): Slide => {
  const name = typeof item.name === 'string' ? item.name : `slide_${String(index).padStart(2, '0')}`
  const error = typeof item.svg_error === 'string' && item.svg_error.trim()
    ? item.svg_error.trim()
    : 'Invalid SVG document.'
  return {
    id: nanoid(10),
    elements: [],
    background: {
      type: 'image',
      image: {
        src: svgToDataUrl(invalidSvgPlaceholderSvg(index, name, error, width, height)),
        size: 'contain',
      },
    },
    remark: typeof item.notes === 'string' ? item.notes : '',
  }
}

const invalidSvgPlaceholderSvg = (index: number, name: string, error: string, width: number, height: number) => {
  const viewWidth = width || 1280
  const viewHeight = height || 720
  const lines = wrapErrorText(error, 86).slice(0, 7)
  const detailLines = lines.map((line, lineIndex) => (
    `<tspan x="96" dy="${lineIndex === 0 ? 0 : 28}">${escapeSvgText(line)}</tspan>`
  )).join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${viewWidth} ${viewHeight}">
  <rect width="${viewWidth}" height="${viewHeight}" fill="#FFF7F7"/>
  <rect x="54" y="54" width="${viewWidth - 108}" height="${viewHeight - 108}" rx="16" fill="#FFFFFF" stroke="#F0B8B8" stroke-width="2"/>
  <text x="96" y="130" fill="#9F2A2A" font-family="Microsoft YaHei, Arial, sans-serif" font-size="30" font-weight="700">SVG 解析失败</text>
  <text x="96" y="178" fill="#5F2931" font-family="Microsoft YaHei, Arial, sans-serif" font-size="18">第 ${index} 页：${escapeSvgText(name)}</text>
  <text x="96" y="242" fill="#2F3A4A" font-family="Consolas, Microsoft YaHei, monospace" font-size="17">${detailLines}</text>
  <text x="96" y="${viewHeight - 94}" fill="#9A6370" font-family="Microsoft YaHei, Arial, sans-serif" font-size="15">请打开对应 SVG 文件查看原始 XML 错误，或重新生成该页。</text>
</svg>`
}

const wrapErrorText = (text: string, size: number) => {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (!normalized) return ['Invalid SVG document.']
  const chunks: string[] = []
  for (let index = 0; index < normalized.length; index += size) {
    chunks.push(normalized.slice(index, index + size))
  }
  return chunks
}

const escapeSvgText = (text: string) => text
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&apos;')

const svgToDataUrl = (svg: string) => {
  const encoded = window.btoa(unescape(encodeURIComponent(svg)))
  return `data:image/svg+xml;base64,${encoded}`
}

const currentDeckPayload = () => ({
  title: title.value,
  width: viewportSize.value,
  height: viewportSize.value * viewportRatio.value,
  theme: theme.value,
  slides: slides.value,
  source: {
    kind: props.options.kind,
    id: props.options.id,
  },
  thumbnails: [],
})

const persistDeckJson = async () => {
  const deckResponse = await fetch(deckEndpoint.value, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(currentDeckPayload()),
  })
  if (!deckResponse.ok) throw new Error(await deckResponse.text())
  return deckResponse.json()
}

const maybeAutoSaveInitialDeck = async (payload: PptistDeckPayload) => {
  if (props.options.kind !== 'templateImport') return
  if (payload.source?.saved_deck === true) return
  try {
    const result = await persistDeckJson()
    props.options.onSaved?.(result)
  }
  catch (err) {
    console.warn('[PPTist] initial deck autosave failed', err)
  }
}

const saveCurrentDeck = async () => {
  setStatus(pptistT('pptist.savingJson'))
  const deckResult = await persistDeckJson()

  setStatus(pptistT('pptist.exportingPptx'))
  await exportPPTX(slides.value, true, true, async blob => {
    const formData = new FormData()
    formData.append('file', blob, `${title.value || 'presentation'}.pptx`)
    const response = await fetch(exportEndpoint.value, {
      method: 'POST',
      body: formData,
    })
    if (!response.ok) throw new Error(await response.text())
    props.options.onSaved?.(await response.json())
  })

  props.options.onSaved?.(deckResult)
  setStatus(pptistT('pptist.saved'), 'success')
  window.setTimeout(() => {
    if (statusText.value === pptistT('pptist.saved')) setStatus('')
  }, 1600)
}

const safeSaveCurrentDeck = async () => {
  try {
    errorText.value = ''
    await saveCurrentDeck()
  }
  catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    setError(pptistT('pptist.saveFailed', { message }))
    throw err
  }
}

const publishPaperHost = () => {
  ;(window as any).__PAPER_PPTIST_HOST__ = {
    save: safeSaveCurrentDeck,
    downloadUrl: paperDownloadUrl.value,
    confirmImport: props.options.onConfirmImport,
    saveBeforeConfirmImport: props.options.saveBeforeConfirmImport,
    confirmImportDisabled: props.options.confirmImportDisabled,
    confirmImportHint: props.options.confirmImportHint,
    cancelImport: props.options.onCancelImport,
    reexportPptx: props.options.kind === 'templateImport' ? undefined : props.options.onReexport,
    deleteRun: props.options.kind === 'templateImport' ? undefined : props.options.onDeleteRun,
  }
}

publishPaperHost()

onMounted(async () => {
  if (appRootRef.value) stopDomI18n = installPptistDomI18n(appRootRef.value, props.options.locale || 'zh')

  if (props.options.controller) {
    props.options.controller.save = safeSaveCurrentDeck
    props.options.controller.exportPptx = safeSaveCurrentDeck
  }
  publishPaperHost()

  try {
    await loadDeck()
  }
  catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    setError(pptistT('pptist.initFailed', { message }))
    slidesStore.setSlides([blankSlide()])
  }
})

onBeforeUnmount(() => {
  stopDomI18n?.()
  const host = (window as any).__PAPER_PPTIST_HOST__
  if (host?.save === safeSaveCurrentDeck) delete (window as any).__PAPER_PPTIST_HOST__
})
</script>

<style lang="scss" scoped>
.paper-pptist-app {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: var(--surface-inset, #f5f6f8);
}

.paper-pptist-status {
  position: absolute;
  top: 44px;
  left: 50%;
  z-index: 20;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transform: translateX(-50%);
  min-width: 180px;
  max-width: 560px;
  padding: 7px 12px;
  border: 1px solid var(--line, #cfd9ec);
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface, #fff) 96%, transparent);
  color: var(--body-color, #2b3d63);
  text-align: center;
  font-size: 13px;
  box-shadow: 0 8px 24px rgba(34, 49, 84, 0.12);
}

.paper-pptist-status-spinner {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  border: 2px solid color-mix(in srgb, var(--body-color, #2b3d63) 22%, transparent);
  border-top-color: var(--body-color, #2b3d63);
  border-radius: 999px;
  animation: paper-pptist-spin 0.8s linear infinite;
}

.paper-pptist-status-check {
  flex: 0 0 auto;
  color: var(--theme-color, #4f6b93);
  font-size: 14px;
  font-weight: 800;
  line-height: 1;
}

.paper-pptist-status.error {
  border-color: #f0b8b8;
  color: #9f2a2a;
}

.paper-pptist-status.success {
  border-color: color-mix(in srgb, var(--theme-color, #4f6b93) 26%, var(--line, #cfd9ec));
  color: var(--body-color, #2b3d63);
}

@keyframes paper-pptist-spin {
  to {
    transform: rotate(360deg);
  }
}

.paper-pptist-screen-portal {
  position: fixed;
  inset: 0;
  z-index: 9999;
  width: 100vw;
  height: 100vh;
  background: #000;
}
</style>

<style lang="scss">
:root[data-theme='dark'] .paper-pptist-app {
  color: var(--body-color);

  .pptist-editor,
  .layout-content,
  .center-body,
  .canvas,
  .canvas-wrapper {
    background-color: var(--surface-inset) !important;
  }

  .editor-header,
  .canvas-tool,
  .thumbnails,
  .toolbar,
  .remark,
  .notes-panel,
  .select-panel,
  .search-panel,
  .symbol-panel,
  .image-lib-panel {
    background-color: var(--surface) !important;
    color: var(--body-color) !important;
    border-color: var(--line) !important;
  }

  .editor-header *,
  .canvas-tool *,
  .toolbar *,
  .thumbnails *,
  .remark * {
    border-color: var(--line);
  }

  .menu-item,
  .handler-item,
  .insert-handler-item,
  .title-text,
  .slide-title,
  .tool-text {
    color: var(--body-color) !important;
  }

  .menu-item .icon,
  .insert-handler-item .icon,
  .handler-item svg,
  .arrow,
  .arrow-btn {
    color: var(--muted-text) !important;
  }

  .menu-item:hover,
  .menu-item.active,
  .handler-item.active,
  .handler-item:not(.disable):hover,
  .insert-handler-item.active,
  .insert-handler-item:not(.group-btn):hover,
  .group-btn:hover,
  .group-btn-main:hover,
  .arrow:hover,
  .title-text:hover {
    background-color: var(--surface-hover) !important;
  }

  input,
  textarea,
  select {
    background-color: var(--surface-strong) !important;
    color: var(--body-color) !important;
    border-color: var(--line) !important;
  }

  .slide-thumbnail,
  .thumbnail,
  .thumbnail-item,
  .page-number,
  .remark-container,
  .popover-content {
    background-color: var(--surface-strong) !important;
    color: var(--body-color) !important;
    border-color: var(--line) !important;
  }
}

:root[data-theme='dark'] .tippy-box[data-theme~='popover'] .popover-content {
  background-color: var(--surface-strong) !important;
  color: var(--body-color) !important;
  border-color: var(--line) !important;
}
</style>
