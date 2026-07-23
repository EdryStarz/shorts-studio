import { useEffect, useRef, useState } from "react";
import {
  api, type Account, type BrowserProfile, type Clip, type Job,
  type OAuthAppConfig, type Publication, type PublishingSettings, type Source,
  type TikTokCreatorInfo,
} from "./api";

const formatTime = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
const formatBytes = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
const APP_TIME_ZONE = "Asia/Tbilisi";

function toUtcIso(localValue: string) {
  return new Date(`${localValue}:00+04:00`).toISOString();
}

function Upload({ onUploaded }: { onUploaded: (source: Source, job: Job) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState(false);
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [youtubeAdult, setYoutubeAdult] = useState(false);
  const [youtubeAdultAccess, setYoutubeAdultAccess] = useState(false);
  const [browser, setBrowser] = useState("firefox");
  const [browsers, setBrowsers] = useState<BrowserProfile[]>([]);
  const [busy, setBusy] = useState(false);
  const [importingYoutube, setImportingYoutube] = useState(false);
  const [importSeconds, setImportSeconds] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    api.youtubeBrowsers().then(items => {
      setBrowsers(items);
      const recommended = items.find(item => item.available && item.recommended) || items.find(item => item.available);
      if (recommended) setBrowser(recommended.name);
    }).catch(() => setBrowsers([]));
  }, []);
  useEffect(() => {
    if (!importingYoutube) return;
    const timer = window.setInterval(() => setImportSeconds(seconds => seconds + 1), 1000);
    return () => window.clearInterval(timer);
  }, [importingYoutube]);
  const submit = async () => {
    if (!file || !rights) return;
    setBusy(true); setError("");
    try {
      const source = await api.upload(file, rights);
      onUploaded(source, await api.process(source.id));
      setFile(null);
      setNotice("Видео добавлено, поиск лучших моментов запущен автоматически.");
    }
    catch (e) { setError(e instanceof Error ? e.message : "Ошибка загрузки"); }
    finally { setBusy(false); }
  };
  const importYouTube = async () => {
    const normalizedUrl = youtubeUrl.trim();
    if (!normalizedUrl || !rights || (youtubeAdult && !youtubeAdultAccess)) return;
    setBusy(true); setImportingYoutube(true); setImportSeconds(0); setError(""); setNotice("");
    try {
      const result = await api.importYoutubeAndProcess(normalizedUrl, browser, rights, youtubeAdult, youtubeAdultAccess);
      onUploaded(result.source, result.job); setYoutubeUrl("");
      setNotice(`«${result.source.title}» импортировано. Статус: ${result.source.import_status}.`);
    }
    catch (e) { setError(e instanceof Error ? e.message : "Ошибка импорта видео"); }
    finally { setBusy(false); setImportingYoutube(false); }
  };
  const selectedBrowser = browsers.find(item => item.name === browser);
  const browserReady = !selectedBrowser || selectedBrowser.available;
  return <section className="upload-panel">
    <div className="eyebrow">Новый материал</div>
    <h2>Превратите длинное видео<br />в сильные короткие истории.</h2>
    <button className="dropzone" onClick={() => input.current?.click()}>
      <span className="upload-icon">↗</span>
      <strong>{file ? file.name : "Выберите видео"}</strong>
      <small>{file ? formatBytes(file.size) : "MP4, MOV, MKV или WebM · до 10 ГБ"}</small>
    </button>
    <input ref={input} hidden type="file" accept="video/*" onChange={e => setFile(e.target.files?.[0] || null)} />
    <label className="rights"><input type="checkbox" checked={rights} onChange={e => setRights(e.target.checked)} />
      <span>У меня есть права на обработку и повторную публикацию этого материала.</span>
    </label>
    <div className="link-import">
      <div className="eyebrow">Ссылка YouTube или Kick</div>
      <input value={youtubeUrl} type="url" inputMode="url" placeholder="https://youtube.com/watch?v=... или https://kick.com/.../videos/..." onChange={e => setYoutubeUrl(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); void importYouTube(); } }} />
      <div className="browser-row">
        <select value={browser} onChange={e => setBrowser(e.target.value)}>
          {(browsers.length ? browsers : [{ name: "firefox", available: true, recommended: true, warning: null } as BrowserProfile]).map(item =>
            <option value={item.name} disabled={!item.available} key={item.name}>
              {item.name === "firefox" ? "Firefox" : item.name === "chrome" ? "Google Chrome" : "Microsoft Edge"}{item.recommended ? " · recommended" : ""}{!item.available ? " · not found" : ""}
            </option>)}
        </select>
        <span className={browserReady ? "connected" : "missing"}>{browserReady ? "Профиль найден" : "Профиль не найден"}</span>
      </div>
      {selectedBrowser?.warning && <small className="browser-warning">{selectedBrowser.warning}</small>}
      <label className="rights"><input type="checkbox" checked={youtubeAdult} onChange={e => { setYoutubeAdult(e.target.checked); if (!e.target.checked) setYoutubeAdultAccess(false); }} />
        <span>Для этого видео YouTube показывает ограничение 18+.</span>
      </label>
      {youtubeAdult && <label className="rights nested"><input type="checkbox" checked={youtubeAdultAccess} onChange={e => setYoutubeAdultAccess(e.target.checked)} />
        <span>Мне есть 18 лет и я законно авторизован(а) для просмотра.</span>
      </label>}
      <button className="secondary link-button" disabled={!youtubeUrl || !browserReady || !rights || (youtubeAdult && !youtubeAdultAccess) || busy} onClick={importYouTube}>
        {importingYoutube ? `Импортируем… ${Math.floor(importSeconds / 60)}:${String(importSeconds % 60).padStart(2, "0")}` : "Импортировать и начать нарезку"}
      </button>
      <small className="link-help">YouTube и записи Kick поддерживаются. Cookies используются только в памяти и не сохраняются Shorts Studio; для закрытого или 18+ видео войдите в выбранном браузере.</small>
    </div>
    {notice && <div className="notice">{notice}</div>}
    {error && <div className="error">{error}</div>}
    <button className="primary" disabled={!file || !rights || busy} onClick={submit}>{busy ? "Загружаем…" : "Добавить в студию"}</button>
  </section>;
}

function JobProgress({ job }: { job: Job }) {
  const labels: Record<string, string> = { probe: "Метаданные", transcribe: "Транскрипция", scenes: "Сцены", score: "Отбор", render: "Монтаж" };
  return <div className="job-card">
    <div className="job-head"><span>Локальная обработка</span><strong>{job.progress}%</strong></div>
    <div className="progress"><i style={{ width: `${job.progress}%` }} /></div>
    <div className="stages">{Object.keys(labels).map(name => {
      const stage = job.stages.find(item => item.name === name);
      return <div className={`stage ${stage?.status || "pending"}`} key={name}><b>{stage?.status === "completed" ? "✓" : "·"}</b>{labels[name]}</div>;
    })}</div>
    {job.error && <div className="error">{job.error}</div>}
  </div>;
}

function ClipCard({ initial, onChange }: { initial: Clip; onChange: (clip: Clip) => void }) {
  const [clip, setClip] = useState(initial);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("split");
  const [error, setError] = useState("");
  const mutate = async (action: () => Promise<Clip>) => {
    setBusy(true); setError("");
    try { const next = await action(); setClip(next); onChange(next); setEditing(false); }
    catch (e) { setError(e instanceof Error ? e.message : "Ошибка"); }
    finally { setBusy(false); }
  };
  return <article className="clip-card">
    {clip.export_path && <video className="clip-preview" controls preload="metadata" src={`/api/clips/${clip.id}/preview`} />}
    <div className="clip-top">
      <span className="score">#{clip.sequence_number}</span>
      <div><strong>{clip.title}</strong><small>{formatTime(clip.start)} — {formatTime(clip.end)} · {Math.round(clip.end - clip.start)} сек.</small></div>
      <button className="ghost" onClick={() => setEditing(!editing)}>{editing ? "Закрыть" : "Править"}</button>
    </div>
    <p className="transcript">{clip.description || clip.subtitle_text || clip.transcript || "Музыкальный или неречевой фрагмент"}</p>
    <div className="clip-tags">{clip.hashtags.map(tag => <span key={tag}>#{tag}</span>)}</div>
    <small className={`clip-state ${clip.processing_status}`}>Статус: {clip.processing_status} · {clip.export_path || "MP4 ещё не готов"}</small>
    {editing && <div className="editor">
      <div className="time-fields"><label>Начало<input type="number" step="0.1" value={clip.start} onChange={e => setClip({ ...clip, start: +e.target.value })} /></label>
      <label>Конец<input type="number" step="0.1" value={clip.end} onChange={e => setClip({ ...clip, end: +e.target.value })} /></label></div>
      <label>Субтитры<textarea value={clip.subtitle_text} onChange={e => setClip({ ...clip, subtitle_text: e.target.value })} /></label>
      <button className="secondary" disabled={busy} onClick={() => mutate(() => api.updateClip(clip))}>Сохранить границы</button>
    </div>}
    <div className="metrics">{Object.entries(clip.score_details).slice(0, 3).map(([name, value]) => <span key={name}>{name.replaceAll("_", " ")} <b>{Math.round(value * 100)}%</b></span>)}</div>
    <div className="clip-actions">
      <select value={mode} onChange={e => setMode(e.target.value)}><option value="crop">Умный crop</option><option value="blur">Размытый фон</option><option value="split">Split: новости + JESUSAVGN</option></select>
      <button className="secondary" disabled={busy} onClick={() => mutate(() => api.render(clip.id, mode))}>{busy ? "Рендер…" : "Перерендерить"}</button>
      {clip.export_path && <a className="download" href={`/api/clips/${clip.id}/download`}>Скачать MP4 ↓</a>}
    </div>
    {error && <div className="error">{error}</div>}
  </article>;
}

const platformNames = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram" } as const;
type Platform = keyof typeof platformNames;

const privacyLabels: Record<string, string> = {
  PUBLIC_TO_EVERYONE: "Все",
  MUTUAL_FOLLOW_FRIENDS: "Взаимные подписчики",
  FOLLOWER_OF_CREATOR: "Подписчики",
  SELF_ONLY: "Только я",
};

function TikTokReviewPanel({ account, clips, enabled, onQueued }: {
  account?: Account; clips: Clip[]; enabled: boolean; onQueued: (item: Publication) => void;
}) {
  const rendered = clips.filter(clip => clip.export_path);
  const [clipId, setClipId] = useState("");
  const [creator, setCreator] = useState<TikTokCreatorInfo | null>(null);
  const [title, setTitle] = useState("");
  const [privacy, setPrivacy] = useState("");
  const [scheduledFor, setScheduledFor] = useState(localDateTime(1));
  const [allowComment, setAllowComment] = useState(false);
  const [allowDuet, setAllowDuet] = useState(false);
  const [allowStitch, setAllowStitch] = useState(false);
  const [commercial, setCommercial] = useState(false);
  const [yourBrand, setYourBrand] = useState(false);
  const [branded, setBranded] = useState(false);
  const [musicConfirmed, setMusicConfirmed] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!clipId && rendered[0]) setClipId(rendered[0].id);
  }, [clipId, rendered]);
  useEffect(() => {
    const clip = rendered.find(item => item.id === clipId);
    if (clip) setTitle(`${clip.title}\n\n${clip.hashtags.map(tag => `#${tag}`).join(" ")}`);
  }, [clipId]);
  useEffect(() => {
    if (!account) { setCreator(null); return; }
    api.tiktokCreatorInfo(account.id).then(setCreator).catch(e => setError(e.message));
  }, [account?.id]);

  const invalidCommercial = commercial && !yourBrand && !branded;
  const invalidPrivateBrand = branded && privacy === "SELF_ONLY";
  const submit = async () => {
    if (!account || !clipId || !creator) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const item = await api.scheduleTikTok({
        clip_id: clipId, account_id: account.id, scheduled_for: toUtcIso(scheduledFor),
        timezone_name: APP_TIME_ZONE, title: title.trim(),
        privacy_level: privacy, allow_comment: allowComment, allow_duet: allowDuet,
        allow_stitch: allowStitch, commercial_content: commercial, your_brand: yourBrand,
        branded_content: branded, music_usage_confirmed: musicConfirmed, user_confirmed: confirmed,
      });
      const confirmedRow = await api.publication(item.id);
      if (confirmedRow.status !== "scheduled" || confirmedRow.timezone_name !== APP_TIME_ZONE) {
        throw new Error("Запись TikTok не подтвердилась повторным чтением из базы.");
      }
      onQueued(confirmedRow); setNotice(`TikTok сохранён в очереди. ID: ${confirmedRow.id}`);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось добавить публикацию TikTok"); }
    finally { setBusy(false); }
  };

  if (!account) return <div className="tiktok-review muted-panel">Подключите TikTok, чтобы открыть обязательный экран проверки публикации.</div>;
  return <div className="tiktok-review">
    <div className="review-heading"><div><div className="eyebrow">TikTok · проверка автора</div><h3>Просмотрите и подтвердите публикацию</h3></div><strong>{creator?.creator_nickname || account.display_name}</strong></div>
    <p className="review-note">TikTok требует, чтобы автор вручную выбрал видимость и настройки каждой публикации. Автоматического значения нет.</p>
    <div className="review-grid">
      <div className="review-preview">
        {clipId && <video controls preload="metadata" src={`/api/clips/${clipId}/download`} />}
        <label>Ролик<select value={clipId} onChange={e => setClipId(e.target.value)}>{rendered.map((clip,index)=><option key={clip.id} value={clip.id}>Клип {index + 1} · {Math.round(clip.end-clip.start)} сек.</option>)}</select></label>
      </div>
      <div className="review-fields">
        <label>Название и хэштеги<textarea maxLength={2200} value={title} onChange={e => setTitle(e.target.value)} /></label>
        <label>Видимость<select value={privacy} onChange={e => setPrivacy(e.target.value)}><option value="">Выберите вручную…</option>{creator?.privacy_level_options.map(option=><option key={option} value={option}>{privacyLabels[option] || option}</option>)}</select></label>
        <label>Время публикации<input type="datetime-local" value={scheduledFor} onChange={e=>setScheduledFor(e.target.value)} /></label>
        <div className="interaction-options">
          <label><input type="checkbox" checked={allowComment} disabled={creator?.comment_disabled} onChange={e=>setAllowComment(e.target.checked)} />Комментарии</label>
          <label><input type="checkbox" checked={allowDuet} disabled={creator?.duet_disabled} onChange={e=>setAllowDuet(e.target.checked)} />Дуэты</label>
          <label><input type="checkbox" checked={allowStitch} disabled={creator?.stitch_disabled} onChange={e=>setAllowStitch(e.target.checked)} />Стич</label>
        </div>
        <label className="rights"><input type="checkbox" checked={commercial} onChange={e=>{setCommercial(e.target.checked);if(!e.target.checked){setYourBrand(false);setBranded(false)}}} /><span>Ролик продвигает бренд, продукт или услугу</span></label>
        {commercial && <div className="interaction-options"><label><input type="checkbox" checked={yourBrand} onChange={e=>setYourBrand(e.target.checked)} />Свой бренд</label><label><input type="checkbox" checked={branded} onChange={e=>setBranded(e.target.checked)} />Брендированный контент</label></div>}
        <label className="rights"><input type="checkbox" checked={musicConfirmed} onChange={e=>setMusicConfirmed(e.target.checked)} /><span>{branded ? "Публикуя, я принимаю TikTok Branded Content Policy и Music Usage Confirmation" : "Публикуя, я принимаю TikTok Music Usage Confirmation"}</span></label>
        <label className="rights"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} /><span>Я просмотрел ролик, подтверждаю права и отправку в аккаунт {creator?.creator_nickname || account.display_name}.</span></label>
        {(invalidCommercial || invalidPrivateBrand) && <div className="error">{invalidCommercial ? "Укажите свой бренд или брендированный контент." : "Брендированный контент нельзя публиковать с видимостью «Только я»."}</div>}
        <button className="primary" disabled={busy || !enabled || !creator || !title.trim() || !privacy || !musicConfirmed || !confirmed || invalidCommercial || invalidPrivateBrand} onClick={submit}>{busy ? "Добавляем…" : "Подтвердить публикацию в TikTok"}</button>
      </div>
    </div>
    {notice && <div className="notice">{notice}</div>}{error && <div className="error">{error}</div>}
  </div>;
}

function localDateTime(minutesFromNow = 10) {
  const date = new Date(Date.now() + minutesFromNow * 60_000);
  const local = new Date(date.getTime() + 4 * 60 * 60_000);
  let value = local.toISOString().slice(0, 16);
  const hour = Number(value.slice(11, 13));
  // Four clips at the default 180-minute interval must fit in the allowed
  // 10:00-23:00 window. Otherwise default to 10:00 on the next local day.
  if (hour < 10) value = `${value.slice(0, 10)}T10:00`;
  if (hour > 14) {
    local.setUTCDate(local.getUTCDate() + 1);
    value = `${local.toISOString().slice(0, 10)}T10:00`;
  }
  return value;
}

function publicationTime(value: string) {
  const zoned = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  return new Date(zoned).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    timeZone: APP_TIME_ZONE,
  });
}

function publicationInputTime(value: string) {
  const zoned = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: APP_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(zoned));
  const fields = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
}

function publicationDayKey(value: string) {
  return publicationInputTime(value).slice(0, 10);
}

function publicationDayLabel(value: string) {
  const zoned = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  return new Date(zoned).toLocaleDateString("ru-RU", {
    weekday: "long", day: "2-digit", month: "long", year: "numeric",
    timeZone: APP_TIME_ZONE,
  });
}

const publicationStatus = (status: string) => ({
  scheduled: "Запланировано",
  processing: "Публикуется",
  published: "Опубликовано",
  failed: "Ошибка",
  dry_run: "Тест завершён",
}[status] || status);

function PublishingPanel({ source, clips }: { source: Source; clips: Clip[] }) {
  const connectedDefaultsApplied = useRef(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [oauthConfigs, setOauthConfigs] = useState<OAuthAppConfig[]>([]);
  const [settings, setSettings] = useState<PublishingSettings | null>(null);
  const [publications, setPublications] = useState<Publication[]>([]);
  const [selected, setSelected] = useState<Record<Platform, boolean>>({ youtube: true, tiktok: false, instagram: false });
  const [scheduledFrom, setScheduledFrom] = useState(localDateTime());
  const [interval, setInterval] = useState(180);
  const [dryRun, setDryRun] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [savingPublication, setSavingPublication] = useState<string | null>(null);
  const [editedTimes, setEditedTimes] = useState<Record<string, string>>({});
  const [publicationErrors, setPublicationErrors] = useState<Record<string, string>>({});
  const [queueScope, setQueueScope] = useState<"all" | "project">("all");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [editingPlatform, setEditingPlatform] = useState<Platform | null>(null);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const refresh = () => Promise.all([
    api.accounts(), api.publishingSettings(), api.publications(), api.oauthConfigs(),
  ]).then(([nextAccounts, nextSettings, nextPublications, nextOauthConfigs]) => {
      setAccounts(nextAccounts); setSettings(nextSettings); setPublications(nextPublications);
      setOauthConfigs(nextOauthConfigs);
      if (!connectedDefaultsApplied.current) {
        connectedDefaultsApplied.current = true;
        const connected = (platform: Platform) => nextAccounts.some(
          account => account.platform === platform && !account.revoked,
        );
        // TikTok keeps its separate mandatory creator-review flow. YouTube and
        // Instagram can be selected together for the automatic queue.
        setSelected({
          youtube: connected("youtube"),
          tiktok: false,
          instagram: connected("instagram"),
        });
      }
    });
  const refreshPublications = async () => {
    const persisted = await api.publications();
    setPublications(persisted);
    return persisted;
  };
  useEffect(() => {
    refresh().catch(e => setError(e.message));
    const timer = window.setInterval(() => refresh().catch(e => setError(e.message)), 15_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data === "shorts-studio-oauth" || event.data?.type === "shorts-studio-oauth") {
        if (event.data?.ok === false) setError(event.data.message || "Ошибка OAuth");
        else setNotice(event.data?.message || "Аккаунт подключён.");
        refresh().catch(e => setError(e.message));
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, []);
  const connect = (platform: Platform) => {
    setError("");
    const popup = window.open(`/api/oauth/${platform}/authorize`, `oauth-${platform}`, "width=620,height=760");
    if (!popup) setError("Браузер заблокировал окно авторизации.");
  };
  const editConfig = (platform: Platform) => {
    const config = oauthConfigs.find(item => item.platform === platform);
    setEditingPlatform(platform); setClientId(config?.client_id || ""); setClientSecret("");
    setError(""); setNotice("");
  };
  const saveConfig = async () => {
    if (!editingPlatform || !clientId.trim()) return;
    setBusy(true); setError("");
    try {
      const current = oauthConfigs.find(item => item.platform === editingPlatform);
      const secret = clientSecret ? clientSecret : current?.secret_configured ? null : "";
      const saved = await api.saveOauthConfig(editingPlatform, clientId.trim(), secret);
      setOauthConfigs(items => [...items.filter(item => item.platform !== saved.platform), saved]);
      setSettings(value => value ? {
        ...value, platforms_configured: { ...value.platforms_configured, [saved.platform]: saved.configured },
      } : value);
      setEditingPlatform(null); setClientSecret("");
      setNotice(saved.configured ? "OAuth-настройки сохранены. Теперь нажмите «Подключить»." : "Client ID сохранён, но для этой платформы также нужен Client Secret.");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось сохранить OAuth-настройки"); }
    finally { setBusy(false); }
  };
  const toggleRealPublishing = async () => {
    setBusy(true); setError("");
    try {
      const next = await api.updatePublishingSettings(!settings?.enabled, !settings?.enabled && confirmed);
      setSettings(next); setNotice(next.enabled ? "Реальная публикация включена." : "Реальная публикация выключена.");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось изменить режим публикации"); }
    finally { setBusy(false); }
  };
  const schedule = async () => {
    const platforms = (Object.keys(selected) as Platform[]).filter(platform => selected[platform]);
    if (!platforms.length) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const accountIds = Object.fromEntries(platforms.map(platform => [
        platform, accounts.find(account => account.platform === platform && !account.revoked)?.id || "",
      ]));
      const queued = await api.batchSchedule({
        source_id: source.id, platforms, scheduled_from: toUtcIso(scheduledFrom),
        interval_minutes: interval, timezone_name: APP_TIME_ZONE,
        dry_run: dryRun, user_confirmed: !dryRun && confirmed, account_ids: accountIds,
      });
      if (!queued.length) throw new Error("API не вернул ни одной созданной публикации.");
      const confirmedRows = await Promise.all(queued.map(item => api.publication(item.id)));
      const ids = new Set(confirmedRows.map(item => item.id));
      if (ids.size !== confirmedRows.length || confirmedRows.some(item =>
        !item.clip_id || !item.platform || !item.scheduled_for
        || item.timezone_name !== APP_TIME_ZONE || item.status !== "scheduled"
      )) throw new Error("Проверка созданных записей очереди через API не пройдена.");
      const persisted = await api.publications();
      if (confirmedRows.some(item => !persisted.some(saved => saved.id === item.id))) {
        throw new Error("Созданная публикация не найдена повторным GET-запросом.");
      }
      setPublications(persisted);
      setNotice(`Подтверждено SQLite/API: ${confirmedRows.length} публикаций. ID: ${confirmedRows.map(item => item.id).join(", ")}`);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать очередь"); }
    finally { setBusy(false); }
  };
  const savePublicationTime = async (publication: Publication) => {
    const localValue = editedTimes[publication.id] || publicationInputTime(publication.scheduled_for);
    setSavingPublication(publication.id); setError(""); setNotice("");
    setPublicationErrors(values => ({ ...values, [publication.id]: "" }));
    try {
      const updated = await api.updatePublicationSchedule(
        publication.id, toUtcIso(localValue), APP_TIME_ZONE,
      );
      const confirmedRow = await api.publication(updated.id);
      if (
        confirmedRow.scheduled_for !== updated.scheduled_for
        || confirmedRow.timezone_name !== APP_TIME_ZONE
      ) throw new Error("Новое время не подтвердилось повторным чтением из базы.");
      const persisted = await refreshPublications();
      const saved = persisted.find(item => item.id === confirmedRow.id);
      if (
        !saved || saved.scheduled_for !== confirmedRow.scheduled_for
        || saved.requested_for !== confirmedRow.scheduled_for
      ) throw new Error("Новое время не сохранилось в SQLite после повторного GET-запроса.");
      setEditedTimes(values => {
        const next = { ...values };
        delete next[publication.id];
        return next;
      });
      setNotice(`Сохранено в базе: ${publicationTime(confirmedRow.scheduled_for)} (${confirmedRow.timezone_name}), ID ${confirmedRow.id}.`);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Не удалось изменить время публикации";
      setPublicationErrors(values => ({ ...values, [publication.id]: message }));
    } finally {
      setSavingPublication(null);
    }
  };
  const removeFromQueue = async (publication: Publication) => {
    if (!window.confirm(`Удалить публикацию ${publication.id} из очереди? Клип и MP4 останутся на месте.`)) return;
    setSavingPublication(publication.id); setPublicationErrors(values => ({ ...values, [publication.id]: "" }));
    try {
      await api.deletePublication(publication.id);
      const persisted = await refreshPublications();
      if (persisted.some(item => item.id === publication.id)) {
        throw new Error("Запись всё ещё присутствует в очереди после повторного GET-запроса.");
      }
      setNotice(`Публикация ${publication.id} удалена только из очереди. Клип и MP4 сохранены.`);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Не удалось удалить публикацию из очереди";
      setPublicationErrors(values => ({ ...values, [publication.id]: message }));
    } finally { setSavingPublication(null); }
  };
  const requeue = async (publication: Publication) => {
    const localValue = editedTimes[publication.id] || localDateTime(30);
    setSavingPublication(publication.id); setPublicationErrors(values => ({ ...values, [publication.id]: "" }));
    try {
      const updated = await api.requeuePublication(publication.id, toUtcIso(localValue), APP_TIME_ZONE);
      const confirmedRow = await api.publication(updated.id);
      if (confirmedRow.status !== "scheduled") throw new Error("Повторная очередь не подтвердилась в базе.");
      setPublications(items => items.map(item => item.id === confirmedRow.id ? confirmedRow : item));
      setNotice(`Публикация ${confirmedRow.id} повторно поставлена на ${publicationTime(confirmedRow.scheduled_for)}.`);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Не удалось повторно поставить публикацию";
      setPublicationErrors(values => ({ ...values, [publication.id]: message }));
    } finally { setSavingPublication(null); }
  };
  const clipIds = new Set(clips.map(clip => clip.id));
  const activeQueue = publications.filter(item => ["scheduled", "processing"].includes(item.status));
  const sourceQueue = activeQueue.filter(item => clipIds.has(item.clip_id));
  const visibleQueue = (queueScope === "project" ? sourceQueue : activeQueue)
    .slice().sort((a, b) => a.scheduled_for.localeCompare(b.scheduled_for));
  const queueGroups = visibleQueue.reduce<Record<string, Publication[]>>((groups, item) => {
    const key = publicationDayKey(item.scheduled_for);
    (groups[key] ||= []).push(item);
    return groups;
  }, {});
  const platformCounts = (Object.keys(platformNames) as Platform[]).map(platform =>
    `${platformNames[platform]}: ${activeQueue.filter(item => item.platform === platform).length}`,
  );
  return <section className="publishing-panel">
    <div className="publishing-head"><div><div className="eyebrow">Автопубликация</div><h2>Очередь YouTube · TikTok · Instagram</h2></div><span>Всего: {activeQueue.length} · {platformCounts.join(" · ")}</span></div>
    <div className="queue-toolbar" role="group" aria-label="Фильтр расписания">
      <button className={queueScope === "all" ? "active" : ""} onClick={() => setQueueScope("all")}>Все публикации</button>
      <button className={queueScope === "project" ? "active" : ""} onClick={() => setQueueScope("project")}>Текущий проект ({sourceQueue.length})</button>
    </div>
    {visibleQueue.length > 0 && <div className="queue-list">
      {Object.entries(queueGroups).map(([day, items]) => <section className="queue-day" key={day}>
      <h3>{publicationDayLabel(items[0].scheduled_for)} <small>{items.length}</small></h3>
      {items.map(item => <article className="queue-row" key={item.id}>
        {item.preview_url ? <video className="queue-preview" controls preload="metadata" src={item.preview_url} /> : <div className="queue-preview empty">Нет превью</div>}
        <div className="queue-copy">
          <div className="queue-title"><strong>Клип {item.clip_number}: {item.clip_title}</strong><span>{platformNames[item.platform as Platform] || item.platform}</span></div>
          <p>{item.clip_description}</p>
          <div className="queue-tags">{item.clip_hashtags.map(tag => <span key={tag}>#{tag}</span>)}</div>
          <small>ID: {item.id}</small>
        </div>
        <div className="queue-schedule">
          {item.status === "scheduled" || item.status === "failed" ? <input
            type="datetime-local"
            aria-label={`Время публикации ${platformNames[item.platform as Platform] || item.platform}`}
            value={editedTimes[item.id] || publicationInputTime(item.scheduled_for)}
            onChange={event => setEditedTimes(values => ({ ...values, [item.id]: event.target.value }))}
          /> : <time>{publicationTime(item.scheduled_for)}</time>}
          <small>Дата и время: {publicationTime(item.scheduled_for)}</small>
          <small>Часовой пояс: {item.timezone_name} · попыток: {item.attempts}</small>
          <span className={`queue-status ${item.status}`}>{publicationStatus(item.status)}</span>
        </div>
        <div className="queue-actions">
          {item.status === "scheduled" && <button
            className="ghost queue-save"
            disabled={savingPublication === item.id}
            onClick={() => savePublicationTime(item)}
          >{savingPublication === item.id ? "Сохраняем…" : "Сохранить время"}</button>}
          {item.status === "scheduled" && <button className="ghost queue-delete" disabled={savingPublication === item.id} onClick={() => removeFromQueue(item)}>Удалить из очереди</button>}
          {item.status === "failed" && <button className="ghost queue-save" disabled={savingPublication === item.id} onClick={() => requeue(item)}>Повторить</button>}
          {item.preview_url && <a className="ghost queue-watch" href={item.preview_url} target="_blank" rel="noreferrer">Смотреть</a>}
        </div>
        {(item.last_error || publicationErrors[item.id]) && <small className="queue-error">{publicationErrors[item.id] || item.last_error}</small>}
      </article>)}
      </section>)}
      <small className="queue-window">Автопланировщик: 10:00–23:00 и интервал от 150 минут. При ручном переносе можно выбрать любое будущее время.</small>
    </div>}
    {visibleQueue.length === 0 && <div className="queue-empty">В выбранном разделе нет запланированных или публикующихся записей.</div>}
    <div className="account-grid">{(Object.keys(platformNames) as Platform[]).map(platform => {
      const account = accounts.find(item => item.platform === platform && !item.revoked);
      const configured = settings?.platforms_configured[platform];
      return <div className="account-card" key={platform}>
        <strong>{platformNames[platform]}</strong>
        <small>{account ? account.display_name : configured ? "OAuth готов — войдите в аккаунт" : "Сначала настройте официальный OAuth"}</small>
        <div className="account-actions"><button className="ghost" onClick={() => editConfig(platform)}>Настроить</button>
        <button className="ghost connect" disabled={!configured || !!account} onClick={() => connect(platform)}>{account ? "Подключено" : "Подключить"}</button></div>
      </div>;
    })}</div>
    <TikTokReviewPanel
      account={accounts.find(item => item.platform === "tiktok" && !item.revoked)}
      clips={clips}
      enabled={!!settings?.enabled}
      onQueued={async () => { await refreshPublications(); }}
    />
    {editingPlatform && (() => {
      const config = oauthConfigs.find(item => item.platform === editingPlatform);
      return <div className="oauth-editor">
        <div><strong>{platformNames[editingPlatform]} OAuth</strong><button className="ghost" onClick={() => setEditingPlatform(null)}>Закрыть</button></div>
        <p>{config?.requirements}</p>
        <a href={config?.setup_url} target="_blank" rel="noreferrer">Открыть кабинет разработчика ↗</a>
        <label>Client ID / Client Key<input value={clientId} onChange={e => setClientId(e.target.value)} autoComplete="off" /></label>
        <label>Client Secret {editingPlatform === "youtube" && <small>необязателен для Desktop app</small>}<input type="password" value={clientSecret} onChange={e => setClientSecret(e.target.value)} placeholder={config?.secret_configured ? "Секрет уже сохранён" : "Введите Client Secret"} autoComplete="new-password" /></label>
        <label>Redirect URI<input readOnly value={config?.redirect_uri || ""} /></label>
        <small>Скопируйте Redirect URI в настройки TikTok/Meta. Ключи шифруются и остаются только на этом компьютере.</small>
        <button className="secondary" disabled={busy || !clientId.trim()} onClick={saveConfig}>{busy ? "Сохраняем…" : "Сохранить OAuth"}</button>
      </div>;
    })()}
    <div className="publish-controls">
      <div className="platform-checks">{(Object.keys(platformNames) as Platform[]).map(platform => <label key={platform}><input type="checkbox" checked={selected[platform]} onChange={e => setSelected({ ...selected, [platform]: e.target.checked })} />{platformNames[platform]}</label>)}</div>
      <label>Начать (Asia/Tbilisi)<input type="datetime-local" value={scheduledFrom} onChange={e => setScheduledFrom(e.target.value)} /></label>
      <label>Интервал, минут<input type="number" min="1" max="10080" value={interval} onChange={e => setInterval(+e.target.value)} /></label>
      <label className="rights"><input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)} /><span>Тестовый режим без отправки</span></label>
      {!dryRun && <label className="rights"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /><span>Я подтверждаю публикацию выбранных клипов в подключённые аккаунты.</span></label>}
      {!dryRun && <button className="secondary" disabled={busy || (!settings?.enabled && !confirmed)} onClick={toggleRealPublishing}>{settings?.enabled ? "Выключить реальные публикации" : "Включить реальные публикации"}</button>}
      <button className="primary" disabled={busy || interval < 1 || !clips.some(clip => clip.export_path) || (!dryRun && (!confirmed || !settings?.enabled))} onClick={schedule}>{busy ? "Создаём очередь…" : "Поставить все клипы в очередь"}</button>
    </div>
    {notice && <div className="notice">{notice}</div>}{error && <div className="error">{error}</div>}
  </section>;
}

export default function App() {
  const [sources, setSources] = useState<Source[]>([]);
  const [active, setActive] = useState<Source | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [clips, setClips] = useState<Clip[]>([]);
  const [error, setError] = useState("");
  const [resultNotice, setResultNotice] = useState("");
  useEffect(() => { api.sources().then(items => { setSources(items); setActive(items[0] || null); }).catch(e => setError(e.message)); }, []);
  useEffect(() => {
    if (!active) { setClips([]); setJob(null); return; }
    let cancelled = false;
    setError(""); setResultNotice("");
    Promise.all([api.clips(active.id), api.latestJob(active.id)]).then(([nextClips, latestJob]) => {
      if (cancelled) return;
      setClips(nextClips); setJob(latestJob);
      if (latestJob?.status === "completed" && nextClips.length === 0) {
        setResultNotice("Анализ завершён, но фрагменты выше порога качества не найдены. Можно запустить повторный анализ кнопкой выше.");
      }
    }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [active?.id]);
  useEffect(() => {
    if (!job || ["completed", "failed"].includes(job.status)) {
      if (job?.status === "completed" && active && job.source_id === active.id) {
        api.clips(active.id).then(items => {
          setClips(items);
          setResultNotice(items.length ? `Готово: найдено ${items.length} лучших фрагментов.` : "Анализ завершён, но подходящие фрагменты не найдены.");
        }).catch(e => setError(e.message));
      }
      return;
    }
    const poll = () => api.job(job.id).then(next => { setJob(next); setError(""); }).catch(e => setError(e.message));
    const timer = window.setInterval(poll, 1500);
    return () => window.clearInterval(timer);
  }, [job, active]);
  const uploaded = (source: Source, nextJob: Job) => { setSources(old => [source, ...old.filter(item => item.id !== source.id)]); setActive(source); setJob(nextJob); setClips([]); setResultNotice(""); };
  const start = async () => { if (active) { setError(""); setResultNotice(""); try { setJob(await api.process(active.id)); } catch (e) { setError(e instanceof Error ? e.message : "Ошибка"); } } };
  return <main>
    <header><div className="brand"><span>S</span><div>SHORTS<strong>STUDIO</strong></div></div><div className="local-pill"><i /> LOCAL AI · GPU AUTO</div></header>
    <div className="shell">
      <aside><Upload onUploaded={uploaded} /><div className="library"><div className="eyebrow">Библиотека · {sources.length}</div>{sources.map(source => <button className={active?.id === source.id ? "active" : ""} key={source.id} onClick={() => setActive(source)}><span>{source.title}</span><small>{source.import_status} · {source.duration ? formatTime(source.duration) : formatBytes(source.size_bytes)}</small></button>)}</div></aside>
      <section className="workspace">
        {!active ? <div className="empty"><span>01</span><h1>Добавьте исходное видео</h1><p>Анализ речи, сцен и динамики выполняется локально — материал не отправляется в AI-сервисы.</p></div> : <>
          <div className="source-head"><div><div className="eyebrow">Активный проект · {active.import_status}</div><h1>{active.title}</h1><p>{active.author && `${active.author} · `}{active.duration ? `${formatTime(active.duration)} · ${active.width}×${active.height}` : "Готов к анализу"}</p></div><button className="primary compact" onClick={start}>Найти лучшие моменты</button></div>
          {job && job.source_id === active.id && <JobProgress job={job} />}
          {resultNotice && <div className="notice">{resultNotice}</div>}
          {error && <div className="error">{error}</div>}
          <PublishingPanel source={active} clips={clips} />
          {clips.length > 0 && <div className="clips-head"><div><div className="eyebrow">Отобранные фрагменты</div><h2>{clips.length} кандидатов</h2></div><span>9:16 · 1080×1920 · H.264</span></div>}
          <div className="clips">{clips.map(clip => <ClipCard initial={clip} key={clip.id} onChange={next => setClips(items => items.map(item => item.id === next.id ? next : item))} />)}</div>
        </>}
      </section>
    </div>
  </main>;
}
