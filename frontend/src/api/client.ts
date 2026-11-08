import type { ApiError, DonorPage, DobroRequest, DobroResponse, IntervalCalendar, MemoRequest, OCRTask } from './types'
const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')
let token = localStorage.getItem('donorflow_token')
export function setToken(value: string | null) { token = value; value ? localStorage.setItem('donorflow_token', value) : localStorage.removeItem('donorflow_token') }
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  if (!response.ok) { let message = `Ошибка запроса (${response.status})`; try { const data = await response.json(); message = data.detail || data.message || message } catch { /* non-json error */ }; const error = new Error(message) as ApiError; error.status = response.status; throw error }
  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') || ''
  return contentType.includes('application/json') ? response.json() : response.blob() as T
}
export const api = {
  donors: (params: { limit: number; offset: number; search?: string }) => { const query = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) }); return request<DonorPage>(`/api/v1/donors?${query}`) },
  intervals: (id: string) => request<IntervalCalendar>(`/api/v1/donors/${id}/intervals`),
  uploadCertificate: (file: File) => { const body = new FormData(); body.append('file', file); return request<OCRTask>('/api/v1/ocr/parse-certificate', { method: 'POST', body }) },
  ocrTask: (id: string) => request<OCRTask>(`/api/v1/ocr/tasks/${id}`),
  memo: (payload: MemoRequest) => request<Blob>('/api/v1/documents/memo/generate', { method: 'POST', body: JSON.stringify(payload) }),
  dobro: (payload: DobroRequest) => request<DobroResponse>('/api/v1/ai/dobro-rf/generate', { method: 'POST', body: JSON.stringify(payload) }),
}
export async function pollOCR(taskId: string, onUpdate: (task: OCRTask) => void) { for (let attempt = 0; attempt < 30; attempt += 1) { const task = await api.ocrTask(taskId); onUpdate(task); if (['success','completed','failed','error'].includes(task.status.toLowerCase())) return task; await new Promise(resolve => setTimeout(resolve, Math.min(1000 + attempt * 350, 5000))) } throw new Error('Распознавание занимает слишком много времени. Проверьте задачу позже.') }
