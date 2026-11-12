// Types mirror the frozen API contract in SPEC.md (backend/server.py).

export type Persona = 'NEW_LAUNCH' | 'STAGNANT_VETERAN' | 'DISTRESSED' | 'UNKNOWN'
export type ContactQuality = 'DIRECT' | 'WEBSITE_ONLY' | 'UNCONTACTABLE'
export type LeadStatus = 'NEW' | 'SHORTLISTED' | 'CONTACTED' | 'SKIP'

export interface Lead {
  id: number
  brand: string
  seller_id: string | null
  seller_country: string | null
  asin: string | null
  title: string | null
  reviews: number | null
  rating: number | null
  persona: Persona
  contact_quality: ContactQuality
  founder_name: string | null
  founder_confidence: number | null
  linkedin_url: string | null
  website: string | null
  email: string | null
  email_confidence: number | null
  email_is_personal: boolean | null
  phone: string | null
  pitch_hook: string | null
  needs_review: boolean
  status: LeadStatus
  verified_at: string | null
}

export interface LeadsResponse {
  total: number
  items: Lead[]
}

export interface Stats {
  leads_total: number
  leads_new_today: number
  runs_last_7d: number
  captcha_rate: number
}

export type RunStatus = 'running' | 'done' | 'failed' | 'partial'

export interface Run {
  id: number
  started_at: string | null
  finished_at: string | null
  status: RunStatus
  keywords_used: string[] | null
  brands_seen: number | null
  leads_new: number | null
  leads_total: number | null
  captcha_blocks: number | null
  errors: string[] | null
  email_status: string | null
  summary_json: Record<string, unknown> | null
}

export interface AuditResult {
  brand: string | null
  asin: string | null
  seller_id: string | null
  seller_country: string | null
  country_confidence: number | null
  pl_score: number | null
  pl_reason: string | null
  catalog: {
    total_products: number | null
    max_reviews: number | null
    all_under_100: boolean
    pages_checked: number | null
    incomplete: boolean
    fail_reason: string | null
  }
  persona: Persona
  listing_problems: string[]
  pitch_hook: string | null
  contacts: Record<string, unknown>
  needs_review: boolean
}

export interface CaptureResponse {
  job_id: number | string
  deep_link: string
}
