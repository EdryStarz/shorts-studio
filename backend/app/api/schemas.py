from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.database.models import JobStatus, PublishStatus, StageStatus


class StageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    status: StageStatus
    error: str | None = None


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source_id: str
    status: JobStatus
    progress: int
    error: str | None = None
    stages: list[StageView] = []


class SourceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    original_name: str
    source_url: str | None = None
    title: str
    author: str
    import_status: str
    import_error: str | None = None
    checksum: str
    size_bytes: int
    duration: float | None
    width: int | None
    height: int | None
    created_at: datetime


class YouTubeImportJobView(BaseModel):
    source: SourceView
    job: JobView


class YouTubeImportRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    browser: str = Field(default="firefox", pattern="^(firefox|chrome|edge)$")
    rights_confirmed: bool
    adult_content: bool = False
    adult_access_confirmed: bool = False


class YouTubeBrowserView(BaseModel):
    name: str
    available: bool
    recommended: bool
    warning: str | None = None


class ClipView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source_id: str
    sequence_number: int
    title: str
    description: str
    hashtags: list[str]
    processing_status: str
    preview_path: str | None
    start: float
    end: float
    score: float
    score_details: dict
    transcript: str
    subtitle_text: str
    export_path: str | None
    subtitle_path: str | None
    checksum: str | None


class ClipUpdate(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    subtitle_text: str = Field(max_length=10000)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if not 15 <= self.end - self.start <= 180:
            raise ValueError("clip duration must be between 15 and 180 seconds")
        return self


class RenderRequest(BaseModel):
    mode: str = Field(default="crop", pattern="^(crop|blur|split)$")
    dynamic_subtitles: bool = True


class ScheduleRequest(BaseModel):
    clip_id: str
    platform: str = Field(pattern="^(youtube|tiktok|instagram)$")
    scheduled_for: datetime
    timezone_name: str = "Asia/Tbilisi"
    dry_run: bool = True
    user_confirmed: bool = False
    account_id: str | None = None


class PublicationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    clip_id: str
    account_id: str | None
    platform: str
    scheduled_for: datetime
    requested_for: datetime | None
    next_attempt_at: datetime | None
    timezone_name: str
    status: PublishStatus
    dry_run: bool
    user_confirmed: bool
    platform_post_id: str | None
    attempts: int
    error_log: list
    publish_metadata: dict
    clip_number: int
    clip_title: str
    clip_description: str
    clip_hashtags: list[str]
    clip_export_path: str | None
    preview_url: str | None
    last_error: str | None


class PublicationScheduleUpdate(BaseModel):
    scheduled_for: datetime
    timezone_name: str = "Asia/Tbilisi"


class TikTokCreatorInfoView(BaseModel):
    creator_username: str
    creator_nickname: str
    creator_avatar_url: str | None = None
    privacy_level_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int


class TikTokPublicationRequest(BaseModel):
    clip_id: str
    account_id: str
    scheduled_for: datetime
    timezone_name: str = "UTC"
    title: str = Field(min_length=1, max_length=2200)
    privacy_level: str = Field(pattern="^(PUBLIC_TO_EVERYONE|MUTUAL_FOLLOW_FRIENDS|FOLLOWER_OF_CREATOR|SELF_ONLY)$")
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    commercial_content: bool = False
    your_brand: bool = False
    branded_content: bool = False
    music_usage_confirmed: bool
    user_confirmed: bool

    @model_validator(mode="after")
    def valid_tiktok_disclosure(self):
        if not self.music_usage_confirmed or not self.user_confirmed:
            raise ValueError("TikTok publication requires explicit creator consent")
        if self.commercial_content and not (self.your_brand or self.branded_content):
            raise ValueError("select at least one commercial content disclosure")
        if not self.commercial_content and (self.your_brand or self.branded_content):
            raise ValueError("commercial content disclosure must be enabled")
        if self.branded_content and self.privacy_level == "SELF_ONLY":
            raise ValueError("branded content cannot be private")
        return self


class AccountView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    platform: str
    external_id: str
    display_name: str
    revoked: bool


class PublishingSettingsUpdate(BaseModel):
    enabled: bool
    user_confirmed: bool = False


class PublishingSettingsView(BaseModel):
    enabled: bool
    platforms_configured: dict[str, bool]


class OAuthAppConfigUpdate(BaseModel):
    client_id: str = Field(min_length=3, max_length=1000)
    client_secret: str | None = Field(default=None, max_length=2000)


class OAuthAppConfigView(BaseModel):
    platform: str
    client_id: str
    secret_configured: bool
    configured: bool
    redirect_uri: str
    setup_url: str
    requirements: str


class BatchScheduleRequest(BaseModel):
    source_id: str
    platforms: list[str] = Field(min_length=1, max_length=3)
    scheduled_from: datetime
    interval_minutes: int = Field(default=150, ge=1, le=10080)
    timezone_name: str = "Asia/Tbilisi"
    dry_run: bool = True
    user_confirmed: bool = False
    account_ids: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_platforms(self):
        allowed = {"youtube", "tiktok", "instagram"}
        if any(platform not in allowed for platform in self.platforms):
            raise ValueError("unsupported publishing platform")
        self.platforms = list(dict.fromkeys(self.platforms))
        return self
