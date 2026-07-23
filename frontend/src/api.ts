export type Source = {
  id: string; original_name: string; checksum: string; size_bytes: number;
  source_url: string | null; title: string; author: string; import_status: string;
  import_error: string | null;
  duration: number | null; width: number | null; height: number | null; created_at: string;
};
export type Stage = { name: string; status: "pending" | "running" | "completed" | "failed"; error?: string };
export type Job = { id: string; source_id: string; status: string; progress: number; error?: string; stages: Stage[] };
export type YouTubeImportJob = { source: Source; job: Job };
export type Clip = {
  id: string; source_id: string; start: number; end: number; score: number;
  sequence_number: number; title: string; description: string; hashtags: string[];
  processing_status: string; preview_path: string | null;
  score_details: Record<string, number>; transcript: string; subtitle_text: string;
  export_path: string | null; checksum: string | null;
};

export type BrowserProfile = {
  name: "firefox" | "chrome" | "edge"; available: boolean;
  recommended: boolean; warning: string | null;
};

export type Account = {
  id: string; platform: "youtube" | "tiktok" | "instagram";
  external_id: string; display_name: string; revoked: boolean;
};

export type PublishingSettings = {
  enabled: boolean;
  platforms_configured: Record<"youtube" | "tiktok" | "instagram", boolean>;
};

export type OAuthAppConfig = {
  platform: "youtube" | "tiktok" | "instagram";
  client_id: string; secret_configured: boolean; configured: boolean;
  redirect_uri: string; setup_url: string; requirements: string;
};

export type Publication = {
  id: string; clip_id: string; platform: string; scheduled_for: string;
  requested_for: string | null; next_attempt_at: string | null; timezone_name: string;
  account_id: string | null;
  status: string; dry_run: boolean; user_confirmed: boolean;
  platform_post_id: string | null; attempts: number; error_log: unknown[];
  publish_metadata: Record<string, unknown>;
  clip_number: number; clip_title: string; clip_description: string;
  clip_hashtags: string[]; clip_export_path: string | null; preview_url: string | null;
  last_error: string | null;
};

export type TikTokCreatorInfo = {
  creator_username: string; creator_nickname: string; creator_avatar_url: string | null;
  privacy_level_options: string[]; comment_disabled: boolean; duet_disabled: boolean;
  stitch_disabled: boolean; max_video_post_duration_sec: number;
};

export type TikTokPublicationPayload = {
  clip_id: string; account_id: string; scheduled_for: string; timezone_name: string;
  title: string; privacy_level: string; allow_comment: boolean; allow_duet: boolean;
  allow_stitch: boolean; commercial_content: boolean; your_brand: boolean;
  branded_content: boolean; music_usage_confirmed: boolean; user_confirmed: boolean;
};

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const method = (options?.method || "GET").toUpperCase();
  let response: Response;
  try {
    response = await fetch(url, options);
  } catch {
    if (method === "GET") {
      await new Promise(resolve => window.setTimeout(resolve, 500));
      try {
        response = await fetch(url, options);
      } catch {
        throw new Error("Связь с локальным сервисом временно потеряна. Shorts Studio продолжит попытки автоматически.");
      }
    } else {
      throw new Error("Локальный сервис не ответил. Видео не потеряно: откройте Shorts Studio снова, и состояние обработки восстановится.");
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || "Ошибка запроса");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  sources: () => request<Source[]>("/api/sources"),
  upload: (file: File, rights: boolean) => {
    const body = new FormData();
    body.append("file", file); body.append("rights_confirmed", String(rights));
    body.append("adult_content", "false"); body.append("adult_access_confirmed", "false");
    return request<Source>("/api/sources", { method: "POST", body });
  },
  youtubeBrowsers: () => request<BrowserProfile[]>("/api/youtube/browsers"),
  importYoutubeAndProcess: (url: string, browser: string, rights: boolean, adultContent: boolean, adultAccessConfirmed: boolean) =>
    request<YouTubeImportJob>("/api/youtube/import-and-process", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url, browser, rights_confirmed: rights, adult_content: adultContent,
        adult_access_confirmed: adultAccessConfirmed,
      }),
    }),
  process: (id: string) => request<Job>(`/api/sources/${id}/process`, { method: "POST" }),
  latestJob: (id: string) => request<Job | null>(`/api/sources/${id}/latest-job`),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  clips: (id: string) => request<Clip[]>(`/api/sources/${id}/clips`),
  updateClip: (clip: Clip) => request<Clip>(`/api/clips/${clip.id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start: clip.start, end: clip.end, subtitle_text: clip.subtitle_text }),
  }),
  render: (id: string, mode: string) => request<Clip>(`/api/clips/${id}/render`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, dynamic_subtitles: true }),
  }),
  accounts: () => request<Account[]>("/api/accounts"),
  oauthConfigs: () => request<OAuthAppConfig[]>("/api/oauth/config"),
  saveOauthConfig: (platform: string, clientId: string, clientSecret: string | null) =>
    request<OAuthAppConfig>(`/api/oauth/${platform}/config`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    }),
  publishingSettings: () => request<PublishingSettings>("/api/publishing/settings"),
  updatePublishingSettings: (enabled: boolean, userConfirmed: boolean) =>
    request<PublishingSettings>("/api/publishing/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, user_confirmed: userConfirmed }),
    }),
  publications: () => request<Publication[]>("/api/publications"),
  publication: (id: string) => request<Publication>(`/api/publications/${id}`),
  updatePublicationSchedule: (id: string, scheduledFor: string, timezoneName: string) =>
    request<Publication>(`/api/publications/${id}/schedule`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scheduled_for: scheduledFor, timezone_name: timezoneName }),
    }),
  deletePublication: (id: string) => request<void>(`/api/publications/${id}`, {
    method: "DELETE",
  }),
  requeuePublication: (id: string, scheduledFor: string, timezoneName: string) =>
    request<Publication>(`/api/publications/${id}/requeue`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scheduled_for: scheduledFor, timezone_name: timezoneName }),
    }),
  tiktokCreatorInfo: (accountId: string) =>
    request<TikTokCreatorInfo>(`/api/tiktok/creator-info/${accountId}`),
  scheduleTikTok: (payload: TikTokPublicationPayload) =>
    request<Publication>("/api/tiktok/publications", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }),
  batchSchedule: (payload: {
    source_id: string; platforms: string[]; scheduled_from: string;
    interval_minutes: number; timezone_name: string; dry_run: boolean;
    user_confirmed: boolean; account_ids: Record<string, string>;
  }) => request<Publication[]>("/api/publications/batch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }),
};
